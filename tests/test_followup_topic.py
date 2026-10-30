"""范围型短追问的连续任务链；原漏测只覆盖了“其他版本”相邻写法。"""
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from doris_rag import web, telemetry, generation
from doris_rag.sessions import SessionStore, prepare_turn, finish_state, resolve_turn
from doris_rag.query_understanding import understand_query
from test_query_pipeline import fake_retriever, row

QUESTION = '列转行和聚合多维分析有什么区别'

class FollowupTopicTests(unittest.TestCase):
    def test_continuous_execution_objects_scope_queries_evidence_and_delivery(self):
        cases = [('其他所有版本区别', ['3.x','2.1']), ('那其他版本呢', ['3.x','2.1']),
                 ('2.1 和 3.x 有什么不同', ['2.1','3.x']),
                 ('所有版本都一样吗', ['4.x','3.x','2.1']),
                 ('换个话题，Iceberg Catalog 有什么变化', ['4.x'])]
        for followup, versions in cases:
            with self.subTest(followup=followup), tempfile.TemporaryDirectory() as tmp:
                db = SessionStore(Path(tmp)/'sessions.sqlite3')
                sid = db.create()['id']
                # 真实会话受理/完成两轮，只有索引与模型换成可核查的离线夹具。
                def run(turn):
                    subject = 'Iceberg Catalog' if '换个话题' in turn['query'] else '列转行和聚合多维分析'
                    engine = fake_retriever([row(subject+'的官方文档说明。',v,n) for n,v in enumerate(('4.x','3.x','2.1'))])
                    def response(messages, **kwargs):
                        self.assertIn(subject, messages[-1]['content'])
                        return subject+'：依据当前送达文档核对，不能编造版本差异。[证据 1]'
                    from doris_rag.pipeline import run_query
                    with patch('doris_rag.pipeline.enrich_context', side_effect=lambda rows,q: [{**r,'contexts':[],'context_status':'complete'} for r in rows]), patch.object(generation,'llm_config',return_value={'model':'fixture','base_url':'fixture','api_key':'fixture'}), patch.object(generation,'chat_completion',side_effect=response):
                        return run_query(turn['query'],turn['version'],analysis_seed=turn['analysis_seed'],version_scope=turn['version_scope'],resolved_scope=turn.get('resolved_scope'),task=turn.get('task'),retriever=engine,mode='dense',fact_sources=turn['fact_sources'])
                with patch.object(telemetry,'TRACE_DIR',Path(tmp)/'traces'), patch.object(web,'index_ready',return_value=True), patch.object(web,'run_query',side_effect=lambda query,version,**kw: run(active)), patch('doris_rag.query_understanding.chat_completion',side_effect=AssertionError('no extra model')):
                    for text in (QUESTION,followup):
                        _, mid, active = db.accept(sid,text)
                        web.complete_turn(db,sid,mid,active,perf_counter(),0)
                    doc=db.get(sid); result=doc['messages'][-1]['result']
                self.assertEqual(result['status'],'generated')
                self.assertEqual(result['understanding']['versions'],versions)
                queries=' '.join(q['text'] for q in result['understanding']['subqueries'])
                if followup.startswith('换个话题'):
                    self.assertEqual(active['kind'],'topic_switch')
                    self.assertNotIn('列转行',queries)
                    self.assertIn('Iceberg Catalog',result['answer'])
                else:
                    self.assertEqual(active['kind'],'follow_up')
                    for subject in ('列转行','聚合多维分析'):
                        self.assertIn(subject,queries)
                        self.assertTrue(all(subject in r['chunk_text'] for r in result['evidence']))
                        self.assertIn(subject,result['answer'])
                trace=__import__('json').loads(Path(result['diagnostics']['trace_path']).read_text(encoding='utf-8'))
                self.assertEqual([e['version'] for e in trace['events'] if e['name']=='retrieval.scope_filter'],versions)
                self.assertEqual(len(doc['messages']),4)

    def test_clarification_does_not_deliver_unconfirmed_tail(self):
        with patch.object(generation,'llm_config',return_value={'model':'fixture','base_url':'fixture','api_key':'fixture'}), patch.object(generation,'chat_completion',return_value='【需要澄清：请确认比较的条件？】\n无关完整答案。[证据 1]'):
            result=generation.answer(QUESTION,[row('官方说明。')],'4.x')
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(result['answer'],'请确认比较的条件？')
        self.assertEqual(result['citation_numbers'],[])

    def test_relative_synonyms_and_new_subject(self):
        first=prepare_turn({},QUESTION)
        state=finish_state(first['state'],{'status':'generated','answer':'fixture','searched_versions':['4.x'],'understanding':understand_query(QUESTION)})
        for text in ('其它全部版本区别','其余版本有何差异','all other versions'):
            t=prepare_turn(state,text)
            self.assertEqual(understand_query(t['query'],version_scope=t['version_scope'],resolved_scope=t.get('resolved_scope'),task=t.get('task'))['versions'],['3.x','2.1'])
        t=prepare_turn(state,'2.1 和 3.x 的 Iceberg Catalog 有什么不同？')
        with patch('doris_rag.query_understanding.chat_completion', return_value='{"relation":"new_question","clarification":""}'):
            t=resolve_turn(t)
        self.assertEqual(t['kind'],'new_question')
        self.assertNotIn('列转行',t['query'])
