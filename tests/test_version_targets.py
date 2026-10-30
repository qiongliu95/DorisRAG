"""冻结Q1–Q6并验证目标范围、Foundation边界与后续校验；不调用真实模型。"""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from doris_rag.query_understanding import understand_query, _foundation_query, ExtractionValidationError
from doris_rag.sessions import prepare_turn, finish_state
from doris_rag.generation import messages
from test_query_pipeline import row

CASES = json.loads((Path(__file__).parent/'fixtures/q1_q6_version_cases.json').read_text(encoding='utf-8'))


def intent(case):
    current = {'Q2': '2.1.2', 'Q4': '2.1.2', 'Q5': '2.0.0', 'Q6': '2.1.2'}.get(case['id'])
    context = []
    if current:
        context.append({'literal': current, 'role': 'current', 'quote': current})
    for literal in (['2.0.0', '2.1.1'] if case['id'] in ('Q4', 'Q6') else ['2.0.0'] if case['id'] == 'Q2' else []):
        context.append({'literal': literal, 'role': 'historical', 'quote': literal})
    if case['id'] == 'Q5':
        context.append({'literal': '2.1.2', 'role': 'planned', 'quote': '2.1.2'})
    return {'targets': [{'literal': v, 'quote': v} for v in case['expected']['target']], 'context': context}


def extraction(case):
    return {'goal': '核对本轮内存文档规则', 'facts': [], 'conditions': [], 'inferences': [], 'missing_conditions': [],
        'foundation': {'base_object': 'BE内存', 'scenario_object': '', 'type_quote': '', 'rule_goal': '定义'},
        'subquestions': [], 'version_intent': intent(case)}


class TargetScopeTests(unittest.TestCase):
    def analyze(self, case, value=None, *, material=None, **kwargs):
        with patch('doris_rag.query_understanding.llm_config', return_value={'api_key': 'fixture'}), patch(
                'doris_rag.query_understanding.chat_completion', return_value=json.dumps(value or extraction(case), ensure_ascii=False)) as model:
            result = understand_query(case['input'] if material is None else material, **kwargs)
        return result, model.call_count

    def test_frozen_six_actual_inputs_and_user_expected_ranges(self):
        for case in CASES:
            with self.subTest(case=case['id']):
                plan, calls = self.analyze(case)
                if case['id'] == 'Q1':
                    self.assertEqual(plan['status'], 'needs_clarification')
                    self.assertIn('2.0.0', plan['clarification'])
                    self.assertNotIn('versions', plan)
                    self.assertEqual(calls, 0)
                else:
                    self.assertEqual(plan['status'], 'ready', plan)
                    self.assertEqual(plan['versions'], case['expected']['supported'])
                    self.assertEqual(plan['missing_version_sources'], case['expected']['missing'])
                    self.assertEqual(plan['requested_version_literals'], case['expected']['target'])
                    self.assertEqual(calls, 1)
                self.assertEqual(plan['raw_query'], case['input'])

    def test_historical_q2_foundation_reaches_all_later_checks(self):
        case = CASES[1]
        original = json.loads(json.loads(case['historical_model_responses'][0])['choices'][0]['message']['content'])
        # 历史提取对应完整会话请求，其中前一轮已明确Memory Tracker，不能用末句冒充当时送模文本。
        self.assertIn('Memory Tracker', _foundation_query(original['foundation'], case['historical_assembled_query']))
        original['version_intent'] = intent(case)
        plan, _ = self.analyze(case, original, material=case['historical_assembled_query'], version_scope=case['input'])
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(len(plan['subqueries']), 3)
        # 此处BE目标已在拆分中，不能为同一技术对象追加重复召回。
        self.assertEqual(plan['subqueries'][0]['purpose'], 'foundation')
        self.assertEqual(plan['extraction_warnings'], [])

    def test_false_fact_type_and_new_subquestion_constraints_still_apply(self):
        case = CASES[1]
        for change in ('fact', 'type', 'parameter', 'version'):
            value = extraction(case)
            if change == 'fact': value['facts'] = [{'quote': '已确认发生内存泄漏'}]
            if change == 'type': value['foundation']['type_quote'] = 'BIGINT'
            if change == 'parameter': value['foundation']['base_object'] = 'invented_option_x'
            if change == 'version': value['foundation']['base_object'] = 'Doris 4.1.4 的参数'
            with self.subTest(change=change):
                self.assertEqual(self.analyze(case, value)[0]['status'], 'understanding_failed')

    def test_forged_version_roles_and_measurements_are_rejected(self):
        case = CASES[5]
        for target in ({'literal': '5.0', 'quote': 'Doris 5.0'}, {'literal': '2.95', 'quote': '2.95 GB'}):
            value = extraction(case)
            value['version_intent']['targets'] = [target]
            self.assertEqual(self.analyze(case, value)[0]['status'], 'understanding_failed')
        value = extraction(case)
        value['version_intent']['targets'] = []
        plan, _ = self.analyze(case, value)
        self.assertEqual(plan['status'], 'needs_clarification')
        self.assertNotIn('versions', plan)

    def test_current_and_planned_target_remain_separate_on_followup(self):
        case = CASES[4]
        turn = prepare_turn({}, case['input'])
        plan, _ = self.analyze(case, version_scope=turn['version_scope'])
        state = finish_state(turn['state'], {'status': 'generated', 'answer': 'fixture', 'understanding': plan})
        self.assertEqual(state['environment_literal'], '2.0.0')
        follow = prepare_turn(state, '那具体如何判断？')
        with patch('doris_rag.query_understanding.chat_completion', side_effect=AssertionError('no extra call')):
            resumed = understand_query(follow['query'], follow['version'], follow['analysis_seed'], version_scope=follow['version_scope'])
        self.assertEqual(resumed['versions'], ['2.1'])
        self.assertTrue(resumed['comprehension_reused'])
        self.assertIn(case['input'], follow['query'])

    def test_partial_comparison_gap_reaches_generation_as_metadata(self):
        plan, _ = self.analyze(CASES[2])
        built = messages(CASES[2]['input'], [row('2.1文档规则', '2.1')], '2.1', plan)
        self.assertIn('"missing_version_sources": ["2.0.0"]', built[1]['content'])
        self.assertIn('"requested_version_literals": ["2.0.0", "2.1.2"]', built[1]['content'])
        self.assertEqual(plan['versions'], ['2.1'])

    def test_source_selection_conflict_is_not_removed(self):
        plan, _ = self.analyze(CASES[4], selected_version='4.x')
        self.assertEqual(plan['status'], 'needs_clarification')
        self.assertIn('冲突', plan['clarification'])


class FoundationBoundaryTests(unittest.TestCase):
    def test_upgrade_reports_in_same_family_do_not_block_explicit_targets(self):
        root = Path(__file__).resolve().parents[1]
        trace = json.loads((root/'logs/traces/20261027T090230273867Z_1890792ec9584250a647d33e2f238ff7.json').read_text(encoding='utf-8'))
        value = json.loads(json.loads(trace['model_calls'][0]['raw_response'])['choices'][0]['message']['content'])
        query = json.loads((root/'reports/version_logic_fix_20261027/a13_input.json').read_text(encoding='utf-8'))['question'].strip()
        with patch('doris_rag.query_understanding.chat_completion', return_value=json.dumps(value, ensure_ascii=False)):
            plan = understand_query(query, version_scope=query)
        # 真实响应还违反既有type_quote长度上限；本批不放宽独立的数据契约。
        self.assertEqual(plan['status'], 'understanding_failed')
        self.assertEqual(plan['failure_reason'], '基础对象/类型/规则提取不完整')
        self.assertEqual(plan['versions'], ['2.1'])
        # 单独验证成功结果的会话持久化，不把上述失败回放冒充真实成功。
        state = finish_state(prepare_turn({}, query)['state'], {'status': 'generated', 'answer': '',
            'understanding': {**plan, 'status': 'ready', 'subqueries': []}})
        self.assertEqual(state['environment_literal'], '2.1.2')
        self.assertEqual(state['environment_version'], '2.1')

    def test_foundation_base_object_and_conditional_mode_still_rejected(self):
        query = 'Doris 2.1.6 INT Range 动态分区；客户端通过JDBC连接Doris后作向量查询。'
        for base, scenario, goal in (
            ('动态分区', '动态分区', '区间规则'),
            ('Range分区', '动态分区', '在动态分区下核对边界'),
            ('Range分区', '动态分区', '在动态分区下比较Range与动态分区的规则'),
            ('客户端连接', '向量查询', '通过向量查询核对连接规则'),
        ):
            with self.subTest(base=base, goal=goal), self.assertRaises(ExtractionValidationError):
                _foundation_query({'base_object': base, 'scenario_object': scenario, 'type_quote': '', 'rule_goal': goal}, query)
        for base, scenario, goal, dtype in (
            ('Range分区', '动态分区', '上下界与整数比较规则', 'INT'),
            ('客户端连接', '向量查询', '地址驱动与连接参数', ''),
        ):
            text = _foundation_query({'base_object': base, 'scenario_object': scenario, 'type_quote': dtype, 'rule_goal': goal}, query)
            self.assertNotIn(scenario, text)


if __name__ == '__main__':
    unittest.main()
