"""重启、并发、失效任务与输出边界；不调用真实模型。"""
import tempfile
import threading
import unittest
from concurrent.futures import Future,ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock,patch

from fastapi.testclient import TestClient
from doris_rag import web,generation
from doris_rag.common import write_json
from doris_rag.sessions import SessionStore,SessionBusy,prepare_turn,finish_state
from doris_rag.query_understanding import understand_query
from doris_rag.pipeline import run_query
from doris_rag.context import enrich_context
from doris_rag.common import sha256_file
from test_query_pipeline import row


class SessionSafety(unittest.TestCase):
    def test_restart_advances_revision_and_stale_completion_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SessionStore(Path(temp)/'s.db');doc=store.create()
            accepted,old_mid,_=store.accept(doc['id'],'PI()是什么？',0)
            recovered=SessionStore(store.path);current=recovered.get(doc['id'])
            self.assertGreater(current['revision'],accepted['revision'])
            self.assertEqual(current['messages'][-1]['status'],'interrupted')
            current,new_mid,_=recovered.accept(doc['id'],'重试',current['revision'])
            store.finish(doc['id'],old_mid,{'status':'generated','answer':'旧任务'})
            self.assertEqual(recovered.get(doc['id'])['messages'][-1]['id'],new_mid)
            self.assertTrue(recovered.get(doc['id'])['busy'])

    def test_version_change_then_failure_does_not_reuse_old_understanding(self):
        first=prepare_turn({},'Doris 3.x 的 enable_insert_strict 有什么作用？')
        state=finish_state(first['state'],{'status':'generated','answer':'fixture','understanding':understand_query(first['query'])})
        changed=prepare_turn(state,'只查 Doris 2.1。')
        state=finish_state(changed['state'],{'status':'execution_failed','answer':'服务故障'})
        follow=prepare_turn(state,'那具体怎么设置？')
        self.assertIsNone(follow['analysis_seed']);self.assertEqual(follow['version'],'2.1')
        self.assertIn(first['state']['goal'],follow['query'])
        self.assertEqual(understand_query(follow['query'],follow['version'],version_scope=follow['version_scope'])['versions'],['2.1'])

    def test_new_definition_during_clarification_clears_old_conditions(self):
        first=prepare_turn({},'Doris 2.1.1 查询后内存没降，如何排查？')
        state=finish_state(first['state'],{'status':'needs_clarification','answer':'请提供内存指标。'})
        changed=prepare_turn(state,'什么是负载均衡？')
        self.assertEqual(changed['kind'],'new_question');self.assertIsNone(changed['version'])
        self.assertNotIn('内存',changed['query'])
        relative=prepare_turn(state,'那这个指标是什么？')
        self.assertEqual(relative['kind'],'clarification_reply')
        vague=prepare_turn({},'帮我看看')
        vague=finish_state(vague['state'],{'status':'needs_clarification','answer':'哪个功能？'})
        self.assertEqual(prepare_turn(vague,'我想知道 PI() 返回什么？')['kind'],'clarification_reply')

    def test_same_session_concurrent_accept_has_single_winner(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SessionStore(Path(temp)/'s.db');doc=store.create();barrier=threading.Barrier(8)
            def accept(_):
                barrier.wait()
                try:store.accept(doc['id'],'PI()是什么？',0);return 'accepted'
                except SessionBusy:return 'busy'
            with ThreadPoolExecutor(8) as pool:out=list(pool.map(accept,range(8)))
            self.assertEqual(out.count('accepted'),1);self.assertEqual(out.count('busy'),7)
            self.assertEqual(len(store.get(doc['id'])['messages']),2)

    def test_independent_sql_does_not_rewrite_previous_topic_version(self):
        first=prepare_turn({},'Doris 2.1.6 动态分区边界出现20250201，写入失败，如何核对？')
        question='我的 Doris 4.x 执行 SQL 失败，SQL如下：\n```sql\nCREATE TABLE test_variant (v VARIANT);\n```\n怎么核对？'
        turn=prepare_turn(first['state'],question)
        self.assertEqual(turn['kind'],'new_question');self.assertEqual(turn['version'],None)
        self.assertEqual(turn['state']['environment_version'],'4.x')
        self.assertNotIn('20250201',turn['query']);self.assertNotIn('动态分区',turn['query'])
        supplement=prepare_turn(first['state'],'补充：Doris 2.1.6，dynamic_partition.enable=true，还是失败，如何核对？')
        self.assertEqual(supplement['kind'],'supplement');self.assertIn('20250201',supplement['query'])


class AdmissionSafety(unittest.TestCase):
    def test_full_queue_rejects_without_writing_user_message_and_releases_slots(self):
        with tempfile.TemporaryDirectory() as temp,patch('doris_rag.web.store',SessionStore(Path(temp)/'s.db')),patch(
                'doris_rag.web.request_slots',threading.BoundedSemaphore(4)):
            futures=[]
            def submitted(*args):
                future=Future();futures.append(future);return future
            with patch('doris_rag.web.workers.submit',side_effect=submitted):
                client=TestClient(web.app);statuses=[]
                for _ in range(8):
                    doc=client.post('/api/sessions').json()
                    response=client.post('/api/sessions/'+doc['id']+'/turns',json={'query':'PI()','revision':0})
                    statuses.append(response.status_code)
                    if response.status_code==503:
                        rejected=client.get('/api/sessions/'+doc['id']).json()
                        self.assertFalse(rejected['busy']);self.assertFalse(rejected['messages'])
                self.assertEqual(statuses,[202]*4+[503]*4)
                with patch('doris_rag.web.run_query') as query:
                    self.assertEqual(client.post('/api/ask',json={'query':'PI()'}).status_code,503);query.assert_not_called()
                for future in futures:future.set_result(None)
                for _ in range(4):self.assertTrue(web.request_slots.acquire(blocking=False))

    def test_accept_and_submit_failure_release_capacity(self):
        with tempfile.TemporaryDirectory() as temp,patch('doris_rag.web.store',SessionStore(Path(temp)/'s.db')),patch(
                'doris_rag.web.request_slots',threading.BoundedSemaphore(1)):
            client=TestClient(web.app)
            self.assertEqual(client.post('/api/sessions/missing/turns',json={'query':'PI()'}).status_code,404)
            doc=client.post('/api/sessions').json()
            self.assertEqual(client.post('/api/sessions/'+doc['id']+'/turns',json={'query':'   '}).status_code,400)
            with patch('doris_rag.web.workers.submit',side_effect=RuntimeError('fixture')):
                self.assertEqual(client.post('/api/sessions/'+doc['id']+'/turns',json={'query':'PI()'}).status_code,503)
            self.assertFalse(client.get('/api/sessions/'+doc['id']).json()['busy'])
            self.assertTrue(web.request_slots.acquire(blocking=False))

    def test_deleted_queued_turn_does_not_call_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            store=SessionStore(Path(temp)/'s.db');doc=store.create();_,mid,turn=store.accept(doc['id'],'PI()',0)
            store.delete(doc['id'])
            with patch('doris_rag.web.run_query') as query:
                web.complete_turn(store,doc['id'],mid,turn,perf_counter(),0)
                query.assert_not_called()


class OutputAndGateSafety(unittest.TestCase):
    def test_late_document_conditions_are_not_starved_by_long_earlier_context(self):
        from doris_rag.documents import DOCS_PATH
        with tempfile.TemporaryDirectory() as temp,patch('doris_rag.context.config',return_value={'source_root':temp}):
            rows=[]
            for name,content in [('long','## 示例\n'+('很长的无关示例说明'*10+'\n')*60),
                                 ('rule','## 使用限制\n只支持DATE/DATETIME。\n## 示例\n相关示例。\n')]:
                p=Path(temp)/DOCS_PATH/'version-3.x'/(name+'.md');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content,encoding='utf-8')
                rows.append({**row('命中内容'),'doc_id':name,'rank':len(rows)+1,'original_path':str(p),'source_sha256':sha256_file(p),
                             'related_context':{'source_start_line':1,'source_end_line':len(content.splitlines())}})
            result=enrich_context(rows,'如何核对使用限制？',max_chars=1200)
            text=''.join(c['text'] for c in result[1]['contexts'])
            self.assertIn('只支持DATE/DATETIME',text)
            self.assertLessEqual(sum(len(c['text']) for r in result for c in r['contexts']),1200)
            self.assertEqual(result[0]['context_status'],'partial')

    def test_missing_model_configuration_keeps_verified_retrieval_as_candidates(self):
        evidence={**row('PI()返回圆周率'),'contexts':[],'retrieval_trace':[{'subquery_id':'q1'}]}
        retrieval=Mock(manifest={'build_id':'fixture'});retrieval.search_plan.return_value=[evidence]
        with patch('doris_rag.pipeline.enrich_context',return_value=[evidence]),patch('doris_rag.generation.llm_config',return_value={'api_key':'','base_url':'','model':''}):
            result=run_query('PI() 返回什么？','3.x',retriever=retrieval)
        self.assertEqual(result['status'],'llm_not_configured')
        self.assertEqual(len(result['evidence']),1)
        self.assertEqual(result['evidence'][0]['evidence_role'],'retrieval_candidate')

    def test_old_acceptance_cannot_validate_new_pointer(self):
        with tempfile.TemporaryDirectory() as temp,patch('doris_rag.web.ROOT',Path(temp)),patch('doris_rag.web.config',return_value={'data_dir':temp}):
            write_json(Path(temp)/'index/current.json',{'build_id':'new'})
            write_json(Path(temp)/'reports/round1_20261023/build_checks.json',{'passed':True,'build_id':'old'})
            write_json(Path(temp)/'reports/round1_20261023/run_status.json',{'phase':'completed','build_id':'old'})
            self.assertFalse(web.index_ready())

    def test_invalid_citation_is_blocked_for_warning_and_clarification(self):
        for text in ('【证据不足】不能确认。[证据 999]','【需要澄清：哪个版本？】条件[证据 999]'):
            with patch('doris_rag.generation.llm_config',return_value={'api_key':'fixture','base_url':'fixture','model':'fixture'}),patch(
                    'doris_rag.generation.chat_completion',return_value=text):
                result=generation.answer('问题',[row('原文')],'3.x')
                self.assertEqual(result['status'],'answer_validation_failed')
                self.assertNotIn('999',result['answer'])

    def test_abnormal_finish_reason_is_not_delivered_and_response_is_closed(self):
        for reason in ('length','content_filter','tool_calls',None):
            response=Mock();response.json.return_value={'choices':[{'finish_reason':reason,'message':{'content':'残留正文'}}]}
            with patch('doris_rag.generation.llm_config',return_value={'api_key':'fixture','base_url':'https://fixture.test','model':'fixture'}),patch(
                    'doris_rag.generation.requests.post',return_value=response):
                with self.assertRaises(generation.IncompleteModelResponse):generation.chat_completion([])
                response.close.assert_called_once()


if __name__=='__main__':unittest.main()
