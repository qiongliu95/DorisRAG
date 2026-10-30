"""真实材料的离线状态/任务回归；没有模型调用。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from doris_rag import web
from doris_rag.query_understanding import understand_query, current_version_report, ensure_task_query
from doris_rag.sessions import prepare_turn, finish_state, SessionStore
from test_query_pipeline import fake_retriever, row

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT/'reports/focused_logic_fix_20261027'


class FocusedLogicTests(unittest.TestCase):
    def test_actual_routine_material_is_supplement_and_meta_reply_is_not_query(self):
        messages = json.loads((FIXTURE/'routine_original_conversation.json').read_text(encoding='utf-8'))['messages']
        first = prepare_turn({}, messages[0]['content'])
        state = finish_state(first['state'], messages[1]['result'])
        title = prepare_turn(state, messages[2]['content'])
        self.assertEqual(title['kind'], 'clarification_reply')
        self.assertEqual(title['state']['goal'], messages[2]['content'])
        state = finish_state(title['state'], messages[3]['result'])
        material = prepare_turn(state, messages[4]['content'])
        self.assertEqual(material['kind'], 'supplement')
        self.assertIn('existing row', material['query'])
        self.assertIn(messages[2]['content'], material['query'])
        state = finish_state(material['state'], messages[5]['result'])
        explanation = prepare_turn(state, messages[6]['content'])
        self.assertEqual(explanation['kind'], 'explanation')
        self.assertTrue(explanation['acknowledgement'])
        for field in ('goal','inputs','facts','last_understanding','environment_version'):
            self.assertEqual(explanation['state'].get(field), state.get(field))
        resumed = prepare_turn(explanation['state'], '那应该怎么排查？')
        self.assertEqual(resumed['kind'], 'follow_up')
        self.assertIn('existing row', resumed['query'])

    def test_explanation_commits_without_retrieval_and_preserves_facts(self):
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.telemetry.TRACE_DIR', Path(temp)/'traces'):
            db = SessionStore(Path(temp)/'session.db')
            doc = db.create()
            doc, mid, turn = db.accept(doc['id'], 'Doris 3.1 的 partial_columns=true 有什么限制？')
            db.finish(doc['id'], mid, {'status':'generated','answer':'prior','evidence':[],
                'understanding': {'status':'ready','goal':'部分列条件','facts':[{'quote':'partial_columns=true'}],
                                  'conditions':[],'subqueries':[],'technical_literals':['partial_columns']}})
            before = db.get(doc['id'])['state']
            doc, mid, turn = db.accept(doc['id'], '这个是我给你的问题的补充过程')
            with patch('doris_rag.web.run_query', side_effect=AssertionError('must not run')), patch(
                    'doris_rag.web.index_ready', side_effect=AssertionError('must not load index')):
                web.complete_turn(db, doc['id'], mid, turn, perf_counter(), 0)
            saved = db.get(doc['id'])
            self.assertEqual(saved['messages'][-1]['status'], 'acknowledged')
            self.assertEqual(saved['messages'][-1]['result']['timings']['llm_calls'], 0)
            self.assertEqual(saved['state']['facts'], before['facts'])

    def test_explanation_with_actual_new_facts_does_not_drop_them(self):
        state = prepare_turn({}, 'Doris 2.1.1 的 PI()有什么用途？')['state']
        turn = prepare_turn(state, '这是补充说明，当前Doris版本是3.1，不用回答')
        self.assertEqual(turn['kind'], 'supplement')
        self.assertEqual(turn['state']['environment_literal'], '3.1')
        self.assertIn(state['goal'], turn['query'])
        self.assertIn('当前Doris版本是3.1', turn['query'])
        self.assertTrue(turn['acknowledgement'])

    def test_explicit_question_correction_and_topic_switch_are_not_meta_ack(self):
        state = prepare_turn({}, 'Routine Load 部分列更新失败，partial_columns=true')['state']
        for text, kind in [('这是补充说明，请继续分析原因？','supplement'),
                           ('纠正，partial_columns=false','correction'),
                           ('换个问题，PI()返回什么？','topic_switch')]:
            turn = prepare_turn(state, text)
            self.assertEqual(turn['kind'], kind)
            self.assertNotIn('acknowledgement', turn)

    def test_full_a13_upgrade_corrects_model_current_and_preserves_raw_data(self):
        case = json.loads((FIXTURE/'a13_frozen.json').read_text(encoding='utf-8'))
        q = case['question']
        with patch('doris_rag.query_understanding.chat_completion', return_value=json.dumps(case['historical_extraction'],ensure_ascii=False)), patch(
                'doris_rag.query_understanding.llm_config',return_value={'fixture':True}):
            turn = prepare_turn({}, q)
            plan = understand_query(turn['query'], version_scope=turn['version_scope'])
        self.assertEqual(plan['status'], 'ready', plan)
        self.assertEqual(plan['versions'], ['2.1'])
        self.assertEqual([v['literal'] for v in plan['version_intent']['context'] if v['role']=='current'], ['2.1.2'])
        self.assertIn(q.strip(), plan['raw_query'])
        for value in ('1.60 GB','1.02 MB','4.00 GB','54.59 GB','15.53 GB'):
            self.assertIn(value, plan['raw_query'])
        state = finish_state(turn['state'], {'status':'generated','answer':'fixture','understanding':plan})
        self.assertEqual(state['environment_literal'], '2.1.2')
        self.assertEqual(plan['subqueries'][-1]['purpose'], 'task')
        self.assertIn('OOM', plan['subqueries'][-1]['text'])
        self.assertEqual(len(plan['subqueries']), 4)

    def test_history_plans_units_and_multiple_environments_do_not_override_current(self):
        for text, literal in [
            ('以前使用Doris 2.0.0，当前使用Doris 2.1.2。','2.1.2'),
            ('我目前使用Doris 2.0.0，准备升级到Doris 2.1.2。','2.0.0'),
            ('We just upgraded to Doris 2.1.2 and still see it. Previously we used version 2.0.0 at idle.','2.1.2')]:
            self.assertEqual(current_version_report(text)['literal'], literal)
        for text in ('计划升级到Doris 3.1', '如果升级到Doris 3.1', '当前内存1.60 GB和1.02 MB',
                     '当前Doris 2.1.2；当前另一集群Doris 3.1'):
            self.assertIsNone(current_version_report(text), text)

    def test_task_query_retains_all_three_subtasks_without_expanding_top_k(self):
        plan = {'status':'ready','kind':'scenario','goal':'排查任务中断','raw_query':'任务中断，核对规则', 'versions':['3.x'],
                'subqueries':[{'id':f'q{i}','text':'核对规则'+str(i),'purpose':'foundation' if i==1 else 'scenario','technical_literals':[]} for i in range(1,4)]}
        original = copy.deepcopy(plan['subqueries'])
        result = ensure_task_query(plan)
        self.assertEqual(result['subqueries'][:3], original)
        self.assertEqual(result['subqueries'][3]['text'], plan['goal'])
        engine = fake_retriever([row('核对规则，任务中断。',number=i) for i in range(6)])
        self.assertLessEqual(len(engine.search_plan(result,5)), 6)

    def test_knowledge_shortcut_and_fabricated_global_goal_stay_bounded(self):
        with patch('doris_rag.query_understanding.chat_completion', side_effect=AssertionError('no call')):
            plan = understand_query('Doris 3.x PI()返回什么？')
        self.assertEqual(len(plan['subqueries']), 1)
        plan.update(kind='scenario',goal='new_fake_parameter 的作用',raw_query='旧对象',
                    subqueries=[{'id':str(i),'text':'旧对象'} for i in range(3)])
        self.assertEqual(len(ensure_task_query(plan)['subqueries']), 3)
        self.assertTrue(plan['extraction_warnings'])

    def test_already_covered_technical_task_does_not_add_duplicate_search(self):
        plan = {'status':'ready','kind':'scenario','goal':'JDBC 连接与SQL查询', 'raw_query':'JDBC 连接与SQL查询',
                'subqueries':[{'id':str(i),'text':text} for i,text in enumerate(('JDBC 连接','SQL查询','客户端配置'))]}
        self.assertEqual(len(ensure_task_query(plan)['subqueries']), 3)


if __name__ == '__main__':
    unittest.main()
