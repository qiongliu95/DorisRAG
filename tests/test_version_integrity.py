"""版本范围必须独立于不可改写的用户数据；全部请求使用本地夹具。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock, patch

from doris_rag import web, telemetry
from doris_rag.pipeline import run_query
from doris_rag.query_understanding import understand_query, version_mentions
from doris_rag.sessions import prepare_turn, finish_state, SessionStore
from test_query_pipeline import row


LOG = '''The memory usage in BE nodes is not going down despite having no queries.
Process Memory Summary:
    low water mark 1.60 GB, warning water mark 3.20 GB.
    Type=load, Used=1.02 MB(1068375 B)
    Type=tc/jemalloc cache, Used=2.95 GB(3166464064 B), Peak=-1.00 B(-1 B)
    MemTrackerLimiter Limit=4.00 GB(4294967296 B)
There is memory unaccounted for comparing process resident memory and trackers.'''


class VersionIntegrityTests(unittest.TestCase):
    def plan(self, turn):
        return understand_query(turn['query'], turn['version'], version_scope=turn['version_scope'])

    def test_log_numbers_have_no_version_semantics(self):
        self.assertEqual(version_mentions(LOG), [])
        self.assertEqual(version_mentions('comparing:\nUsed=1.73\nPeak=4.27'), [])
        self.assertEqual(version_mentions('Doris 2.1.1 比较内存 1.23 GiB、4.56 MB 和耗时 3.20 ms'),
                         [{'literal': '2.1.1', 'family': '2.1', 'supported_source': True}])

    def test_version_comparisons_upgrades_and_other_software_keep_existing_scope(self):
        for query, versions in (
            ('2.1与3.x行为有什么不同？', ['2.1', '3.x']),
            ('比较 Doris 2.1 和3.x，耗时从1.60秒降为1.02秒', ['2.1', '3.x']),
            ('Hive Catalog 升级到 4.1 后 schema.cache.ttl-second 应如何迁移？', ['4.x']),
            ('Doris3.0.2 的参数是什么？', ['3.x']),
            ('Different MySQL 5.1 drivers for Doris 3.x', ['3.x']),
        ):
            with self.subTest(query=query):
                self.assertEqual(understand_query(query)['versions'], versions)
        self.assertEqual(understand_query('Doris 2.0 的参数')['status'], 'needs_clarification')

    def test_clarification_reply_preserves_every_original_character(self):
        first = prepare_turn({}, LOG)
        previous = finish_state(first['state'], {'status': 'needs_clarification', 'answer': '请确认Doris版本。'})
        untouched = copy.deepcopy(previous)
        turn = prepare_turn(previous, '2.1.1')
        self.assertEqual(previous, untouched)
        self.assertEqual(turn['state']['goal'], LOG)
        self.assertEqual(turn['state']['inputs'], [LOG, '2.1.1'])
        self.assertEqual(turn['fact_sources'], [LOG, '2.1.1'])
        self.assertIn(LOG, turn['query'])
        for quantity in ('1.60 GB', '1.02 MB', '-1.00 B', '4.00 GB'):
            # 架构整改消除goal与inputs重复；原始日志仍逐字保留且送入一次。
            self.assertEqual(turn['query'].count(quantity), 1)
        self.assertNotIn('2.1.1 GB', turn['query'])
        with patch('doris_rag.query_understanding.llm_config', return_value={'api_key': ''}), patch(
                'doris_rag.query_understanding.chat_completion') as llm:
            plan = self.plan(turn)
        llm.assert_not_called()
        self.assertEqual(plan['versions'], ['2.1'])
        self.assertEqual(plan['status'], 'understanding_unavailable')

    def test_version_change_and_temporary_comparison_do_not_rewrite_material(self):
        question = 'Doris 3.0.2 的参数是什么？观测数值 3.0.2，内存 1.60 GB。'
        first = prepare_turn({}, question)
        changed = prepare_turn(first['state'], '纠正一下，是 Doris 2.1.4')
        self.assertEqual(changed['state']['inputs'][0], question)
        self.assertEqual(changed['state']['goal'], question)
        self.assertIn(question, changed['query'])
        self.assertEqual(self.plan(changed)['versions'], ['2.1'])
        comparison = prepare_turn(changed['state'], '3.x和4.x区别在哪')
        self.assertIn(question, comparison['query'])
        self.assertEqual(self.plan(comparison)['versions'], ['3.x', '4.x'])
        follow = prepare_turn(comparison['state'], '那具体含义呢？')
        self.assertEqual(self.plan(follow)['versions'], ['2.1'])

    def test_explicit_scope_keeps_historical_versions_as_unchanged_material(self):
        query = '以前用 Doris 2.0，现在用 Doris 2.1.1，内存是多少？'
        plan = understand_query(query, version_scope='Doris 2.1.1')
        self.assertEqual(plan['versions'], ['2.1'])
        self.assertEqual(plan['raw_query'], query)
        # 明确比较含缺失版本时，保留支持部分并携带缺口，不再整题阻断。
        extraction = {'goal': '核对内存规则', 'facts': [], 'conditions': [], 'inferences': [], 'missing_conditions': [],
            'foundation': {'base_object': '内存', 'scenario_object': '', 'type_quote': '', 'rule_goal': '定义'}, 'subquestions': [],
            'version_intent': {'targets': [{'literal': v, 'quote': v} for v in ('2.0', '2.1')], 'context': []}}
        with patch('doris_rag.query_understanding.llm_config', return_value={'api_key': 'fixture'}), patch(
                'doris_rag.query_understanding.chat_completion', return_value=json.dumps(extraction, ensure_ascii=False)):
            partial = understand_query(query, version_scope='比较 Doris 2.0 与2.1')
        self.assertEqual(partial['status'], 'ready')
        self.assertEqual(partial['versions'], ['2.1'])
        self.assertEqual(partial['missing_version_sources'], ['2.0'])

    def test_actual_generation_payload_contains_original_log_and_scope(self):
        turn = prepare_turn(finish_state(prepare_turn({}, LOG)['state'],
            {'status': 'needs_clarification', 'answer': '请确认版本'}), '2.1.1')
        extracted = {'goal': '解释内存统计', 'facts': [{'quote': 'low water mark 1.60 GB'}],
            'conditions': [], 'inferences': [], 'missing_conditions': [],
            'foundation': {'base_object': '内存', 'scenario_object': 'jemalloc', 'type_quote': '', 'rule_goal': '定义与统计口径'},
            'subquestions': []}
        evidence = {**row('内存统计定义', '2.1'), 'contexts': [], 'retrieval_trace': [{'subquery_id': 'q1'}]}
        retriever = Mock(manifest={'build_id': 'fixture'})
        retriever.search_plan.return_value = [evidence]
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': '内存需要分口径核对。[证据 1]'}}]}
        response.text = json.dumps(response.json.return_value)
        with tempfile.TemporaryDirectory() as directory, patch('doris_rag.telemetry.TRACE_DIR', Path(directory)), patch(
                'doris_rag.query_understanding.chat_completion', return_value=json.dumps(extracted, ensure_ascii=False)) as extract, patch(
                'doris_rag.generation.llm_config', return_value={'api_key': 'fixture', 'base_url': 'https://fixture.invalid', 'model': 'fixture'}), patch(
                'doris_rag.query_understanding.llm_config', return_value={'api_key': 'fixture'}), patch(
                'doris_rag.pipeline.enrich_context', side_effect=lambda rows, query: rows), patch(
                'doris_rag.generation.requests.post', return_value=response) as post:
            result = run_query(turn['query'], turn['version'], fact_sources=turn['fact_sources'],
                               version_scope=turn['version_scope'], retriever=retriever)
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(retriever.search_plan.call_args.args[0]['versions'], ['2.1'])
        self.assertEqual(extract.call_args.args[0][1]['content'], turn['query'])
        sent = post.call_args.kwargs['json']['messages'][1]['content']
        self.assertIn(LOG, sent)
        self.assertNotIn('2.1.1 GB', sent)
        self.assertIn('"versions": ["2.1"]', sent)
        self.assertEqual(result['understanding']['facts'][0]['quote'], 'low water mark 1.60 GB')

    def test_web_session_passes_scope_and_persists_original_material(self):
        with tempfile.TemporaryDirectory() as directory, patch('doris_rag.telemetry.TRACE_DIR', Path(directory)):
            db = SessionStore(Path(directory)/'session.db')
            doc = db.create()
            doc, mid, first = db.accept(doc['id'], LOG, doc['revision'])
            db.finish(doc['id'], mid, {'status': 'needs_clarification', 'answer': '请确认版本'})
            doc = db.get(doc['id'])
            doc, mid, turn = db.accept(doc['id'], '2.1.1', doc['revision'])
            with patch('doris_rag.web.index_ready', return_value=True), patch('doris_rag.web.run_query',
                    return_value={'status': 'generated', 'answer': 'fixture', 'evidence': []}) as run:
                web.complete_turn(db, doc['id'], mid, turn, perf_counter(), 0)
            self.assertEqual(run.call_args.kwargs['version_scope'], 'Doris 版本 2.1.1')
            saved = db.get(doc['id'])
            self.assertEqual(saved['state']['inputs'], [LOG, '2.1.1'])
            self.assertEqual(saved['state']['raw_inputs'], [LOG, '2.1.1'])
            self.assertFalse(saved['busy'])


if __name__ == '__main__':
    unittest.main()
