"""V1交互状态、原请求重试及展示；全部使用隔离数据，不调用模型。"""
import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient
from doris_rag import web, telemetry
from doris_rag.sessions import SessionStore, SessionBusy
from doris_rag.presentation import present_session, present_result, excerpt


class UXTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=SessionStore(Path(self.tmp.name)/'sessions.db')
        self.addCleanup(patch.stopall)
        patch.object(telemetry,'TRACE_DIR',Path(self.tmp.name)/'traces').start()

    def failure(self,text='Doris 3.x 的 PI() 是什么？'):
        doc=self.db.create();doc,mid,turn=self.db.accept(doc['id'],text,0)
        self.db.finish(doc['id'],mid,{'status':'execution_failed','answer':'失败','error_type':'ReadTimeout'})
        return self.db.get(doc['id']),mid,turn

    def test_retry_reuses_exact_query_and_scope_without_adding_messages(self):
        doc,mid,first=self.failure()
        self.db.retry(doc['id'],mid,doc['revision'])
        self.db.finish(doc['id'],mid,{'status':'generated','answer':'fixture','understanding':{'status':'ready','versions':['3.x']}})
        doc,mid,turn=self.db.accept(doc['id'],'那所有版本都查一下',self.db.get(doc['id'])['revision'])
        self.db.finish(doc['id'],mid,{'status':'execution_failed','answer':'失败'})
        failed=self.db.get(doc['id']);doc,_,retry=self.db.retry(doc['id'],mid,failed['revision'])
        self.assertEqual(len(doc['messages']),4)
        for key in ('query','version','version_scope','analysis_seed','fact_sources','state'):
            self.assertEqual(retry[key],turn[key])
        self.assertNotEqual(retry['attempt_id'],turn['attempt_id'])
        self.assertEqual(doc['messages'][-1]['id'],mid)

    def test_concurrent_retry_and_stale_tab_have_single_winner(self):
        doc,mid,_=self.failure();barrier=threading.Barrier(6)
        def retry(_):
            barrier.wait()
            try:self.db.retry(doc['id'],mid,doc['revision']);return True
            except SessionBusy:return False
        with ThreadPoolExecutor(6) as pool:out=list(pool.map(retry,range(6)))
        self.assertEqual(sum(out),1);self.assertEqual(len(self.db.get(doc['id'])['messages']),2)

    def test_old_execution_cannot_commit_over_retried_message(self):
        doc,mid,old=self.failure();_,_,new=self.db.retry(doc['id'],mid,doc['revision'])
        self.db.finish(doc['id'],mid,{'status':'generated','answer':'迟到旧结果'},attempt_id=old['attempt_id'])
        self.assertTrue(self.db.get(doc['id'])['busy'])
        self.db.finish(doc['id'],mid,{'status':'generated','answer':'新结果'},attempt_id=new['attempt_id'])
        self.assertEqual(self.db.get(doc['id'])['messages'][-1]['content'],'新结果')

    def test_restart_retains_snapshot_failure_and_manual_title(self):
        doc,mid,_=self.failure();doc=self.db.rename(doc['id'],'中文会话标题',doc['revision'])
        self.db.retry(doc['id'],mid,doc['revision'])
        reopened=SessionStore(self.db.path);doc=reopened.get(doc['id'])
        self.assertEqual(doc['messages'][-1]['status'],'interrupted')
        self.assertEqual(doc['title'],'中文会话标题')
        self.assertTrue(present_session(doc)['messages'][-1]['retry_available'])
        reopened.retry(doc['id'],mid,doc['revision'])
        self.assertEqual(len(reopened.get(doc['id'])['messages']),2)

    def test_legacy_snapshot_missing_is_not_guessed(self):
        doc,mid,_=self.failure()
        with self.db.connect() as db:
            doc.pop('retry_turn');self.db._save(db,doc)
        self.assertFalse(present_session(doc)['messages'][-1]['retry_available'])
        with self.assertRaises(ValueError):self.db.retry(doc['id'],mid,doc['revision'])

    def test_chinese_errors_follow_actual_failure(self):
        for error,expected in [(requests.ReadTimeout('secret'),'响应超时'),(requests.ConnectionError('secret'),'无法连接'),
                               (requests.ConnectTimeout('secret'),'连接生成服务超时')]:
            result=web.execution_failure(error)
            self.assertIn(expected,result['answer']);self.assertNotIn('secret',result['answer'])
            self.assertNotIn(type(error).__name__,result['answer']);self.assertNotIn('本地配置',result['answer'])
        response=requests.Response();response.status_code=503
        self.assertIn('暂时不可用',web.execution_failure(requests.HTTPError(response=response))['answer'])
        legacy={'status':'execution_failed','answer':'处理未完成（ReadTimeout）。请检查本地配置。',
                'diagnostics':{'failure':{'error_type':'ReadTimeout'}}}
        self.assertNotIn('ReadTimeout',present_result(legacy)['answer_html'])
        self.assertIn('响应超时',present_result(legacy)['answer_html'])

    def test_excerpt_is_real_paragraph_not_fenced_sql_or_a_fake_support_sentence(self):
        self.assertEqual(excerpt('# 标题\n\n```sql\nSELECT 1;\n```'),'')
        self.assertEqual(excerpt('# 标题\n\n这是真实原文。\n\n```sql\nSELECT 1;\n```'),'这是真实原文。')

    def test_rename_api_and_safe_retry_admission(self):
        doc,mid,_=self.failure()
        with patch.object(web,'store',self.db),patch.object(web,'request_slots',threading.BoundedSemaphore(1)):
            client=TestClient(web.app)
            response=client.patch('/api/sessions/'+doc['id'],json={'title':'新的中文标题','revision':doc['revision']})
            self.assertEqual(response.status_code,200);rev=response.json()['revision']
            self.assertEqual(client.patch('/api/sessions/'+doc['id'],json={'title':'覆盖','revision':doc['revision']}).status_code,409)
            future=Future()
            with patch.object(web.workers,'submit',return_value=future) as submit:
                url=f"/api/sessions/{doc['id']}/messages/{mid}/retry"
                self.assertEqual(client.post(url,json={'revision':rev}).status_code,202)
                self.assertIn(client.post(url,json={'revision':rev}).status_code,(409,503))
                self.assertEqual(submit.call_count,1)
                future.set_result(None)
            self.assertEqual(len(self.db.get(doc['id'])['messages']),2)

    def test_titles_wait_for_effective_question_and_manual_name_is_not_overwritten(self):
        doc=self.db.create();doc,mid,_=self.db.accept(doc['id'],'帮我看看',0)
        self.assertEqual(doc['title'],'新会话')
        self.db.finish(doc['id'],mid,{'status':'needs_clarification','answer':'哪个功能？'})
        doc,mid,_=self.db.accept(doc['id'],'PI() 返回什么？',self.db.get(doc['id'])['revision'])
        self.assertIn('PI()',doc['title']);self.assertLessEqual(len(doc['title']),27)
        self.db.finish(doc['id'],mid,{'status':'generated','answer':'fixture'})
        doc=self.db.rename(doc['id'],'手工标题',self.db.get(doc['id'])['revision'])
        doc,_,_=self.db.accept(doc['id'],'换个问题，什么是负载均衡？',doc['revision'])
        self.assertEqual(doc['title'],'手工标题')
