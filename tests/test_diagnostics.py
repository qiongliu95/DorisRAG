"""完全本地的观测数据流验证：禁止 requests 发出网络请求，不加载真实编码模型。"""
import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from doris_rag import telemetry, generation, web
from doris_rag.pipeline import run_query
from doris_rag.sessions import SessionStore
from test_query_pipeline import row, fake_retriever


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch('doris_rag.telemetry.TRACE_DIR', self.directory).start()
        patch('requests.sessions.Session.request', side_effect=AssertionError('network_forbidden')).start()
        patch('doris_rag.generation.llm_config', return_value={
            'model': 'local-fixture', 'base_url': 'https://fixture.invalid/v1', 'api_key': 'fixture-service-secret'}).start()

    def response(self, text='结论[证据 1]', finish='stop', status=200):
        response = Mock(status_code=status)
        response.json.return_value = {'id': 'fixture-response', 'model': 'local-fixture',
            'choices': [{'finish_reason': finish, 'message': {'content': text}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
        response.text = json.dumps(response.json.return_value, ensure_ascii=False)
        if status >= 400:
            response.raise_for_status.side_effect = ValueError('private credential must not escape')
        return response

    def read(self, result):
        return json.loads(Path(result['diagnostics']['trace_path']).read_text(encoding='utf-8'))

    def execute(self, rows, query='target', detailed=True, enrich=None, top_k=2):
        retriever = fake_retriever(rows)
        with patch('doris_rag.pipeline.enrich_context', side_effect=enrich or (lambda rs, q: [{**r, 'contexts': []} for r in rs])), patch(
                'doris_rag.generation.requests.post', return_value=self.response()) as post:
            result = run_query(query, '3.x', top_k, retriever=retriever, diagnostic=detailed)
        return result, self.read(result), post

    def test_raw_channels_fusion_selection_actual_request_and_citations_are_distinct(self):
        rows = [row('target first', number=0), row('target second', number=1)]
        result, trace, post = self.execute(rows)
        names = {e['name'] for e in trace['events']}
        self.assertTrue({'retrieval.raw_dense', 'retrieval.raw_bm25', 'retrieval.raw_exact',
            'retrieval.channel_rrf', 'retrieval.group_pool', 'retrieval.subquery_version_rrf',
            'retrieval.substantive_filter', 'retrieval.primary_selection'}.issubset(names))
        self.assertEqual(len(trace['model_calls'][0]['evidence_units']), 2)
        self.assertEqual(len(trace['result']['evidence']), 1)
        self.assertEqual(trace['model_calls'][0]['request']['messages'], post.call_args.kwargs['json']['messages'])
        self.assertIn('raw_response', trace['model_calls'][0])
        self.assertTrue(telemetry.evidence_path(trace, rows[1]['chunk_id'])['present_in_model_input'])
        self.assertEqual(trace['result']['answer'], result['answer'])
        self.assertIn('config_sha256', trace['identity'])
        self.assertEqual(trace['identity']['index']['build_id'], 'synthetic-unit-fixture')

    def test_first_loss_is_substantive_filter_or_faq_selection(self):
        heading, fact = row('## target', number=0), row('target fact', number=1)
        _, trace, _ = self.execute([heading, fact], top_k=1)
        path = telemetry.evidence_path(trace, heading['chunk_id'])
        self.assertEqual(path['first_missing_before_model']['stage'], 'retrieval.substantive_filter')
        question = 'target无法获取位置。'
        faq = row('**问题描述**：' + question + '\n常见原因：网络。', number=0)
        neighbor = row('**问题描述**：target其他故障。', number=1)
        _, trace, _ = self.execute([faq, neighbor], question)
        path = telemetry.evidence_path(trace, neighbor['chunk_id'])
        self.assertEqual(path['first_missing_before_model']['stage'], 'augmentation.selected_raw')
        self.assertEqual(path['first_missing_before_model']['reason'], 'exact_faq_description')

    def test_nonprimary_chunk_recovered_as_context_is_not_reported_lost(self):
        first, second = row('target first', number=0), row('target second', number=1)
        first.update(source_start_line=1, source_end_line=1)
        second.update(source_start_line=2, source_end_line=2)
        def enrich(rs, query):
            return [{**r, 'contexts': [{'kind': 'related', 'source_start_line': 1, 'source_end_line': 2,
                'source_sha256': 'test', 'complete': True, 'text': 'target first\ntarget second\n'}]} for r in rs]
        _, trace, _ = self.execute([first, second], enrich=enrich, top_k=1)
        path = telemetry.evidence_path(trace, second['chunk_id'])
        self.assertIsNotNone(path['first_primary_exclusion'])
        self.assertTrue(path['present_in_model_input'])
        self.assertEqual(path['model_input_role'], 'context')
        self.assertIsNone(path['first_missing_before_model'])

    def test_light_mode_has_no_messages_body_raw_response_or_user_material(self):
        result, trace, _ = self.execute([row('UNIQUE_SOURCE_TEXT', number=0)], 'UNIQUE_USER_MATERIAL', detailed=False)
        saved = json.dumps(trace, ensure_ascii=False)
        self.assertNotIn('UNIQUE_SOURCE_TEXT', saved)
        self.assertNotIn('UNIQUE_USER_MATERIAL', saved)
        self.assertNotIn('raw_response', saved)
        self.assertNotIn('request', trace['model_calls'][0])
        options = next(e for e in trace['events'] if e['name'] == 'request.input')
        self.assertEqual(options['top_k'], 2)
        self.assertEqual(options['requested_version'], '3.x')
        self.assertEqual(options['retrieval_mode'], 'hybrid_precise')
        self.assertEqual(telemetry.evidence_path(trace, 'any')['status'], 'insufficient_trace')
        self.assertFalse(result['diagnostics']['detail_mode'])

    def test_failure_keeps_dense_before_bm25_exception_and_does_not_call_model(self):
        retriever = fake_retriever([row('target', number=0)])
        retriever.lexical = Mock()
        retriever.lexical.ranked.side_effect = RuntimeError('password=do-not-log-this')
        with patch('doris_rag.generation.requests.post') as post:
            with self.assertRaises(RuntimeError):
                run_query('target', '3.x', retriever=retriever, diagnostic=True)
        trace = json.loads(next(self.directory.glob('*_*.json')).read_text(encoding='utf-8'))
        self.assertTrue(trace['failure']['stage'].endswith('/bm25'))
        self.assertTrue(trace['last_completed_stage'].endswith('/dense'))
        self.assertIn('retrieval.raw_dense', [e['name'] for e in trace['events']])
        self.assertNotIn('do-not-log-this', json.dumps(trace))
        post.assert_not_called()

    def test_truncation_and_http_failure_keep_raw_response_and_generation_rows(self):
        retriever = fake_retriever([row('target', number=0)])
        with patch('doris_rag.pipeline.enrich_context', side_effect=lambda rs, q: [{**r, 'contexts': []} for r in rs]), patch(
                'doris_rag.generation.requests.post', return_value=self.response('PARTIAL_BODY', 'length')):
            result = run_query('target', '3.x', retriever=retriever, diagnostic=True)
        trace = self.read(result)
        self.assertEqual(result['status'], 'generation_incomplete')
        self.assertIn('PARTIAL_BODY', trace['model_calls'][0]['raw_response'])
        self.assertIn('generation.input_rows', [e['name'] for e in trace['events']])
        self.assertIsNotNone(trace['failure'])
        self.assertNotIn('PARTIAL_BODY', result['answer'])
        with telemetry.request_trace(True) as trace:
            with patch('doris_rag.generation.requests.post', return_value=self.response(status=403)):
                with self.assertRaises(ValueError):
                    generation.chat_completion([{'role': 'user', 'content': 'fixture'}])
            trace.finish({'status': 'execution_failed'})
        self.assertEqual(trace.data['model_calls'][0]['http_status'], 403)
        self.assertIn('raw_response', trace.data['model_calls'][0])

    def test_credentials_are_redacted_without_mutating_actual_payload_or_result(self):
        message = 'api_key="user-secret-123" Bearer bearer-secret-123 fixture-service-secret'
        result, trace, post = self.execute([row(message, number=0)])
        saved = json.dumps(trace)
        self.assertNotIn('fixture-service-secret', saved)
        self.assertNotIn('user-secret-123', saved)
        self.assertNotIn('bearer-secret-123', saved)
        self.assertIn(message, post.call_args.kwargs['json']['messages'][1]['content'])
        self.assertEqual(result['answer'], '结论[证据 1]')

    def test_recording_failure_is_visible_and_does_not_change_answer(self):
        with patch.object(telemetry.Path, 'mkdir', side_effect=OSError('private')):
            result, _, _ = self.execute_without_read()
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['diagnostics']['recording_error'], 'OSError')

    def execute_without_read(self):
        retriever = fake_retriever([row('target')])
        with patch('doris_rag.pipeline.enrich_context', side_effect=lambda rs, q: [{**r, 'contexts': []} for r in rs]), patch(
                'doris_rag.generation.requests.post', return_value=self.response()) as post:
            return run_query('target', '3.x', retriever=retriever), None, post

    def test_async_session_opt_in_and_commit_failure_are_linked(self):
        from concurrent.futures import ThreadPoolExecutor
        workers = ThreadPoolExecutor(2)
        patch('doris_rag.web.workers', workers).start()
        self.addCleanup(workers.shutdown, wait=True)
        store = SessionStore(self.directory / 'sessions.db')
        with patch('doris_rag.web.store', store), patch('doris_rag.web.index_ready', return_value=True), patch(
                'doris_rag.web.load_retriever', return_value=fake_retriever([row('target', '4.x')])), patch(
                'doris_rag.pipeline.enrich_context', side_effect=lambda rs, q: [{**r, 'contexts': []} for r in rs]), patch(
                'doris_rag.generation.requests.post', return_value=self.response()):
            client = TestClient(web.app)
            doc = client.post('/api/sessions').json()
            response = client.post('/api/sessions/' + doc['id'] + '/turns', json={'query': 'target', 'diagnostic': True})
            self.assertEqual(response.status_code, 202)
            import time
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                doc = client.get('/api/sessions/' + doc['id']).json()
                if not doc['busy']:
                    break
                time.sleep(.02)
            self.assertFalse(doc['busy'])
            result = doc['messages'][-1]['result']
            self.assertEqual(result['status'], 'generated')
            trace = self.read(result)
            self.assertEqual(trace['identity']['message_id'], response.json()['message_id'])
            self.assertEqual(trace['identity']['session_id'], doc['id'])
            self.assertTrue(trace['detail_mode'])
            self.assertNotIn('diagnostic', doc['state'])
            # 关系判断先于故障提交；该阶段需返回JSON，不能复用答案正文夹具。
            with patch.object(store, 'finish', side_effect=OSError('not public')), patch(
                    'doris_rag.query_understanding.chat_completion',
                    return_value='{"relation":"follow_up","clarification":""}'):
                accepted, mid, turn = store.accept(doc['id'], 'target', doc['revision'])
                with self.assertRaises(OSError):
                    web.complete_turn(store, doc['id'], mid, turn, time.perf_counter(), 0)
            failed = [json.loads(p.read_text(encoding='utf-8')) for p in self.directory.glob('*.json') if not p.name.startswith('code-')]
            self.assertTrue(any(t.get('failure', {}).get('stage') == 'delivery.session_commit'
                                for t in failed if t.get('failure')))

    def test_concurrent_trace_scopes_do_not_mix_or_delete_old_files(self):
        from concurrent.futures import ThreadPoolExecutor
        old = self.directory / ('a' * 32 + '.json')
        untouched = self.directory / 'operator-note.json'
        old.write_text('{}'); untouched.write_text('{}')
        os.utime(old, (1, 1)); os.utime(untouched, (1, 1))
        def execute(owner):
            with telemetry.request_trace(True, {'owner': owner}) as trace:
                telemetry.event('owner', owner)
                trace.finish({'status': 'fixture'})
                return trace.path
        with ThreadPoolExecutor(2) as pool:
            paths = list(pool.map(execute, ['one', 'two']))
        self.assertNotEqual(paths[0], paths[1])
        for path, owner in zip(paths, ['one', 'two']):
            trace = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(trace['events'][0]['data'], owner)
        self.assertTrue(old.exists())
        self.assertTrue(untouched.exists())

    def test_channel_fusion_pool_cutoff_is_observable_before_global_selection(self):
        rows = [row('target first', number=0), row('target second', number=1)]
        retriever = fake_retriever(rows)
        retriever.lexical = Mock()
        retriever.lexical.ranked.return_value = [(1, 10.0)]
        retriever.lexical.exact_ranked.return_value = []
        with telemetry.request_trace(True) as trace:
            found = retriever._rank('target', '3.x', 1, 'hybrid_precise', [])
            trace.finish({'status': 'fixture'})
        self.assertEqual(len(found), 1)
        path = telemetry.evidence_path(trace.data, rows[1]['chunk_id'])
        self.assertEqual(path['first_primary_exclusion']['stage'], 'retrieval.group_pool')
        self.assertIsNone(path['present_in_model_input'])

    def test_http_gate_failure_returns_trace_id_without_model_call(self):
        with patch('doris_rag.web.index_ready', return_value=False), patch('doris_rag.generation.requests.post') as post:
            response = TestClient(web.app).post('/api/ask', json={'query': 'target', 'diagnostic': True})
        self.assertEqual(response.status_code, 503)
        request_id = response.headers['X-DorisRAG-Trace-ID']
        trace = json.loads(Path(response.headers['X-DorisRAG-Trace-Path']).read_text(encoding='utf-8'))
        self.assertEqual(trace['trace_id'], request_id)
        self.assertEqual(trace['failure']['stage'], 'ingestion.index_load')
        self.assertIn('config_sha256', trace['identity'])
        post.assert_not_called()

    def test_default_formal_pipeline_saves_one_light_trace_without_opt_in(self):
        result, _, post = self.execute_without_read()
        trace = self.read(result)
        self.assertFalse(trace['detail_mode'])
        self.assertRegex(Path(result['diagnostics']['trace_path']).name,
                         r'^\d{8}T\d{12}Z_[a-f0-9]{32}\.json$')
        self.assertEqual(len(list(self.directory.glob('*_*.json'))), 1)
        self.assertNotIn('request', trace['model_calls'][0])
        self.assertEqual(trace['model_calls'][0]['messages_sha256'],
                         telemetry.fingerprint(post.call_args.kwargs['json']['messages']))
        self.assertEqual(result['diagnostics']['save_status'], 'saved')

    def test_clarification_refusal_and_exception_keep_distinct_session_links(self):
        from time import perf_counter
        store = SessionStore(self.directory / 'sessions.db')
        doc = store.create()
        paths = []
        for query, outcome, error in [('帮我看看', 'needs_clarification', None),
                                      ('换个问题，PI()用途？', 'no_evidence', None),
                                      ('换个问题，ABS()用途？', 'execution_failed', RuntimeError('Cookie: do-not-save'))]:
            doc, mid, turn = store.accept(doc['id'], query, doc['revision'])
            with patch('doris_rag.web.index_ready', return_value=True), patch(
                    'doris_rag.web.load_retriever', return_value=fake_retriever([row('## target', '4.x')])), patch(
                    'doris_rag.pipeline.enrich_context', return_value=[]), patch(
                    'doris_rag.web.run_query', wraps=run_query, side_effect=error):
                web.complete_turn(store, doc['id'], mid, turn, perf_counter(), 0)
            doc = store.get(doc['id'])
            result = doc['messages'][-1]['result']
            self.assertEqual(result['status'], outcome)
            trace = self.read(result)
            self.assertEqual(trace['status'], outcome)
            self.assertEqual(trace['identity']['session_id'], doc['id'])
            self.assertEqual(trace['identity']['message_id'], mid)
            self.assertEqual(trace['result']['answer_sha256'], telemetry.fingerprint(result['answer']))
            if error:
                self.assertEqual(trace['failure']['error_type'], 'RuntimeError')
                self.assertNotIn('do-not-save', json.dumps(trace))
            paths.append(result['diagnostics']['trace_path'])
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual(len(list(self.directory.glob('*_*.json'))), 3)

    def test_http_default_and_pipeline_share_the_same_trace(self):
        with patch('doris_rag.web.load_retriever', return_value=fake_retriever([row('target', '4.x')])), patch(
                'doris_rag.pipeline.enrich_context', side_effect=lambda rs, q: [{**r, 'contexts': []} for r in rs]), patch(
                'doris_rag.generation.requests.post', return_value=self.response()):
            response = TestClient(web.app).post('/api/ask', json={'query': 'target'})
        self.assertEqual(response.status_code, 200)
        trace = self.read(response.json())
        self.assertFalse(trace['detail_mode'])
        self.assertEqual(trace['result']['status'], 'generated')
        self.assertEqual(len(list(self.directory.glob('*_*.json'))), 1)

    def test_cookie_redaction_covers_body_headers_raw_json_and_known_secret(self):
        with telemetry.request_trace(True) as trace:
            trace.secrets.append('fixture-secret')
            trace.event('input', {'Cookie': 'sid=one; token=two', 'headers': {'X-API-Key': 'secret-three'},
                'text': 'Cookie: sid=four; csrf=five\nAuthorization: Basic basic-secret\napi_key=secret-six'})
            trace.finish({'status': 'generated', 'answer': 'fixture-secret'})
        saved = trace.path.read_text(encoding='utf-8')
        for secret in ('sid=one', 'token=two', 'secret-three', 'sid=four', 'csrf=five', 'basic-secret', 'secret-six', 'fixture-secret'):
            self.assertNotIn(secret, saved)

    def test_session_write_failure_does_not_fail_normal_answer(self):
        from time import perf_counter
        store = SessionStore(self.directory / 'session.db')
        doc = store.create()
        doc, mid, turn = store.accept(doc['id'], 'PI()用途？')
        blocked = self.directory / 'not_a_directory'
        blocked.write_text('fixture')
        with patch('doris_rag.telemetry.TRACE_DIR', blocked), patch('doris_rag.web.index_ready', return_value=True), patch(
                'doris_rag.web.run_query', return_value={'status': 'generated', 'answer': 'fixture answer', 'evidence': []}):
            web.complete_turn(store, doc['id'], mid, turn, perf_counter(), 0)
        result = store.get(doc['id'])['messages'][-1]['result']
        self.assertEqual(result['answer'], 'fixture answer')
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['diagnostics']['save_status'], 'failed')
        self.assertIsNone(result['diagnostics']['trace_path'])

    def test_external_eval_capture_disables_duplicate_files(self):
        with telemetry.request_trace(False, {'entrypoint': 'eval'}, persist=False) as trace:
            result, _, _ = self.execute_without_read()
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['diagnostics']['save_status'], 'disabled')
        self.assertIsNone(result['diagnostics']['trace_path'])
        self.assertFalse(trace.saved)
        self.assertFalse(list(self.directory.glob('*.json')))

    def test_worker_submission_failure_is_saved_without_running_question(self):
        store = SessionStore(self.directory / 'sessions.db')
        doc = store.create()
        with patch('doris_rag.web.store', store), patch('doris_rag.web.workers.submit', side_effect=RuntimeError('fixture')), patch(
                'doris_rag.web.run_query') as run:
            response = TestClient(web.app).post('/api/sessions/' + doc['id'] + '/turns', json={'query': 'PI()用途？'})
        self.assertEqual(response.status_code, 503)
        result = store.get(doc['id'])['messages'][-1]['result']
        trace = self.read(result)
        self.assertEqual(trace['failure']['stage'], 'delivery.worker_submit')
        self.assertEqual(trace['status'], 'execution_failed')
        self.assertEqual(len(list(self.directory.glob('*_*.json'))), 1)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
