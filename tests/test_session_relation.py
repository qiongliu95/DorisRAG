"""关系未确认不得清空状态；按完整Session链验证，模型与索引仅在离线用夹具替代。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import requests
from doris_rag import web, telemetry, generation
from doris_rag.sessions import SessionStore, prepare_turn, resolve_turn
from test_query_pipeline import row, fake_retriever

QUESTION = '列转行和聚合多维分析有什么区别'
SOURCES = Path(__file__).parent/'fixtures'/'session_relation_cases.json'


class SessionRelationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        self.db = SessionStore(Path(self.tmp.name)/'session.db')
        patch.object(telemetry,'TRACE_DIR',Path(self.tmp.name)/'traces').start()
        patch('requests.sessions.Session.request',side_effect=AssertionError('offline_only')).start()

    def initial(self):
        doc = self.db.create()
        _, mid, turn = self.db.accept(doc['id'], QUESTION)
        self.execute(doc['id'], mid, turn)
        return self.db.get(doc['id'])

    def execute(self, sid, mid, turn, relation=None, error=None):
        rows=[]
        for version in ('4.x','3.x','2.1'):
            for subject in ('列转行','聚合多维分析','hive.recursive_directories','BloomFilter'):
                r=row(subject+'的独立定义、条件和示例。',version,len(rows))
                r.update(title=subject,section_path=subject,doc_id=subject+version,contexts=[])
                rows.append(r)
        def answer(messages, **kwargs):
            content=messages[-1]['content']
            # 此处只验证真实生产代码组装了正确请求，不把夹具答案当业务验收。
            self.assertIn('本轮回答任务(JSON)：',content)
            return '离线回答，需另做真实验收。[证据 1]'
        with patch.object(web,'index_ready',return_value=True), patch.object(web,'load_retriever',return_value=fake_retriever(rows)), patch(
                'doris_rag.pipeline.enrich_context',side_effect=lambda rs,q:rs), patch.object(
                generation,'llm_config',return_value={'model':'fixture','base_url':'fixture','api_key':'fixture'}), patch.object(
                generation,'chat_completion',side_effect=answer), patch('doris_rag.query_understanding.chat_completion',
                side_effect=error,return_value=json.dumps({'relation':relation,'clarification':''})):
            turn['diagnostic']=True
            web.complete_turn(self.db,sid,mid,turn,perf_counter(),0)
        return self.db.get(sid)

    def test_same_session_sequences_A_B_C_D(self):
        for case in json.loads(SOURCES.read_text(encoding='utf-8')):
            with self.subTest(case=case['id']):
                sid=self.db.create()['id']
                for text, expected in zip(case['inputs'],case['expected']):
                    _,mid,turn=self.db.accept(sid,text)
                    doc=self.execute(sid,mid,turn,expected['kind'])
                    message=doc['messages'][-1];r=message['result'];p=r['understanding']
                    self.assertEqual(message['turn_kind'],expected['kind'])
                    self.assertEqual(r['status'],'generated',r)
                    self.assertEqual(p['comparison_objects'],expected['objects'])
                    self.assertEqual(p['versions'],expected['versions'])
                    self.assertEqual(p['raw_query'],text)
                    for obj in expected.get('subjects',expected['objects']):
                        self.assertIn(obj,' '.join(q['text'] for q in p['subqueries']))
                    for obj in expected.get('excluded',[]):
                        self.assertNotIn(obj,p['material_query'])
                    trace=json.loads(Path(r['diagnostics']['trace_path']).read_text(encoding='utf-8'))
                    self.assertEqual([e['version'] for e in trace['events'] if e['name']=='retrieval.scope_filter'],expected['versions'])
                    sent=next(e['data'] for e in trace['events'] if e['name']=='generation.input_rows')
                    for obj in expected.get('subjects',expected['objects']):
                        self.assertTrue(any(obj in row['chunk_text'] for row in sent),obj)
                    for path in case.get('forbidden_source_paths',[]):
                        self.assertFalse(any(path in row.get('doc_id','') for row in sent))
                self.assertEqual(len(doc['messages']),len(case['inputs'])*2)

    def test_CONV006_captured_real_responses_follow_actual_session_pipeline(self):
        """冻结真实响应/候选，仅替代外部计算；状态与任务由原始六轮逐步产生。"""
        runtime=json.loads((SOURCES.parent/'CONV006_runtime_v1.json').read_text(encoding='utf-8'))
        case=next(c for c in json.loads(SOURCES.read_text(encoding='utf-8')) if c['id']=='CONV006')
        rows=list(runtime['source_chunks'].values());index={r['chunk_id']:i for i,r in enumerate(rows)}
        sid=self.db.create()['id']
        for frozen,expected in zip(runtime['turns'],case['expected']):
            with self.subTest(input=frozen['input']):
                groups=iter(frozen['rank_groups']);relations=iter(frozen['relation_responses'])
                engine=fake_retriever(copy.deepcopy(rows));sent=[]
                def rank(query,version,*args):
                    group=next(groups)
                    self.assertEqual(version,group['version'])
                    self.assertEqual(telemetry.fingerprint(query),group['query_sha256'])
                    return [{'index':index[r['chunk_id']],'score':r['score'],'rank':n,'channel_hits':[]}
                            for n,r in enumerate(group['rows'],1)]
                engine._rank=rank
                def deliver(messages,**kwargs):
                    sent.append(copy.deepcopy(messages))
                    return frozen['answer_response']
                _,mid,turn=self.db.accept(sid,frozen['input'])
                with patch.object(web,'index_ready',return_value=True),patch.object(web,'load_retriever',return_value=engine),patch.object(
                        generation,'llm_config',return_value={'model':'captured','base_url':'fixture','api_key':'fixture'}),patch.object(
                        generation,'chat_completion',side_effect=deliver),patch('doris_rag.query_understanding.chat_completion',
                        side_effect=lambda *a,**kw:next(relations)):
                    turn['diagnostic']=True
                    web.complete_turn(self.db,sid,mid,turn,perf_counter(),0)
                doc=self.db.get(sid);message=doc['messages'][-1];result=message['result'];plan=result['understanding']
                self.assertEqual(message['turn_kind'],expected['kind'])
                self.assertEqual(plan['raw_query'],frozen['input'])
                self.assertEqual(plan['comparison_objects'],expected['objects'])
                self.assertEqual(plan['versions'],expected['versions'])
                self.assertEqual(result['answer'],frozen['expected_delivery'])
                self.assertEqual(len(sent),1)
                self.assertEqual(list(groups),[]);self.assertEqual(list(relations),[])
                trace=json.loads(Path(result['diagnostics']['trace_path']).read_text(encoding='utf-8'))
                evidence=next(e['data'] for e in trace['events'] if e['name']=='generation.input_rows')
                for subject in expected['subjects']:
                    self.assertIn(subject,' '.join(q['text'] for q in plan['subqueries']))
                    # 对象别名以真实官方文档内容核验，不向 Session 注入正确主题。
                    terms={'列转行':['LATERAL','lateral'],'聚合多维分析':['GROUPING','ROLLUP','CUBE']}.get(subject,[subject])
                    self.assertTrue(any(any(t.lower() in r['chunk_text'].lower() for t in terms) for r in evidence),subject)
                self.assertTrue(all(r['source_version'] in expected['versions'] for r in evidence))
                for path in case['forbidden_source_paths']:
                    self.assertFalse(any(path in r['doc_id'] for r in evidence))
        self.assertEqual(len(doc['messages']),12)

    def test_pending_accept_and_model_execution_preserve_active_state_without_db_lock(self):
        initial=self.initial();before=copy.deepcopy(initial['state'])
        doc,mid,turn=self.db.accept(initial['id'],'通常如何选择？')
        self.assertEqual(turn['kind'],'unresolved')
        self.assertEqual(doc['state'],before)
        def decide(*args,**kwargs):
            self.assertEqual(self.db.get(doc['id'])['state'],before)
            with self.db.connect() as db:
                db.execute('BEGIN IMMEDIATE')
            return '{"relation":"follow_up","clarification":""}'
        with patch('doris_rag.query_understanding.chat_completion',side_effect=decide):
            resolved=self.db.resolve(doc['id'],mid,turn)
        self.assertEqual(resolved['task']['comparison_objects'],['列转行','聚合多维分析'])
        self.assertEqual(resolved['task']['raw_query'],'通常如何选择？')
        self.assertEqual(self.db.get(doc['id'])['retry_turn']['task'],resolved['task'])

    def test_model_cannot_replace_raw_input_objects_or_scope(self):
        before=self.initial()['state'];turn=prepare_turn(before,'通常如何选择？')
        with patch('doris_rag.query_understanding.chat_completion',return_value=json.dumps({
                'relation':'follow_up','clarification':'','goal':'unrelated','comparison_objects':['fake'],'versions':['2.1']})):
            resolved=resolve_turn(turn)
        self.assertEqual(resolved['task']['goal'],QUESTION)
        self.assertEqual(resolved['task']['comparison_objects'],before['comparison_objects'])
        self.assertEqual(resolved['resolved_scope']['versions'],['4.x'])
        self.assertEqual(resolved['fact_sources'][-1],'通常如何选择？')

    def test_relation_network_failure_preserves_state_and_safe_retry_has_no_duplicate_messages(self):
        doc=self.initial();before=copy.deepcopy(doc['state'])
        _,mid,turn=self.db.accept(doc['id'],'通常如何选择？')
        failed=self.execute(doc['id'],mid,turn,error=requests.ReadTimeout('fixture'))
        self.assertEqual(failed['state'],before)
        self.assertEqual(failed['messages'][-1]['status'],'execution_failed')
        _,same,retry=self.db.retry(doc['id'],mid,failed['revision'])
        self.assertEqual(same,mid);self.assertTrue(retry['relation_pending'])
        done=self.execute(doc['id'],mid,retry,'follow_up')
        self.assertEqual(len(done['messages']),4)
        self.assertEqual(done['messages'][-1]['status'],'generated')
        self.assertEqual(done['state']['goal'],QUESTION)

    def test_uncertain_relation_does_not_retrieve_or_mutate_state(self):
        doc=self.initial();before=copy.deepcopy(doc['state'])
        _,mid,turn=self.db.accept(doc['id'],'其中另一个的那个方面')
        with patch('doris_rag.query_understanding.chat_completion',return_value='{"relation":"needs_clarification","clarification":"你要继续讨论哪个对象的哪个方面？"}'), patch.object(
                web,'run_query',side_effect=AssertionError('must_not_retrieve')):
            web.complete_turn(self.db,doc['id'],mid,turn,perf_counter(),0)
        done=self.db.get(doc['id'])
        self.assertEqual(done['messages'][-1]['status'],'needs_clarification')
        self.assertEqual(done['state'],before)

    def test_malformed_relation_does_not_clear_state_or_run_query(self):
        doc=self.initial();before=copy.deepcopy(doc['state'])
        _,mid,turn=self.db.accept(doc['id'],'通常如何选择？')
        with patch('doris_rag.query_understanding.chat_completion',return_value='{}'),patch.object(
                web,'run_query',side_effect=AssertionError('must_not_retrieve')):
            web.complete_turn(self.db,doc['id'],mid,turn,perf_counter(),0)
        self.assertEqual(self.db.get(doc['id'])['state'],before)
        self.assertEqual(self.db.get(doc['id'])['messages'][-1]['status'],'execution_failed')

    def test_explicit_switch_correction_and_relative_scope_do_not_add_relation_call(self):
        state=self.initial()['state']
        with patch('doris_rag.query_understanding.chat_completion',side_effect=AssertionError('no_relation_call')):
            for text,kind in [('换个话题，ABS()返回什么？','topic_switch'),('不是列转行，而是窗口函数','correction'),('其他版本呢？','follow_up')]:
                resolved=resolve_turn(prepare_turn(state,text))
                self.assertEqual(resolved['kind'],kind)
        self.assertEqual(resolved['resolved_scope']['versions'],['3.x','2.1'])

    def test_late_relation_cannot_commit_after_session_deletion(self):
        doc=self.initial();_,mid,turn=self.db.accept(doc['id'],'通常如何选择？')
        def decide(*args,**kwargs):
            self.db.delete(doc['id'])
            return '{"relation":"follow_up","clarification":""}'
        with patch('doris_rag.query_understanding.chat_completion',side_effect=decide):
            self.assertIsNone(self.db.resolve(doc['id'],mid,turn))
        with self.assertRaises(KeyError):self.db.get(doc['id'])
