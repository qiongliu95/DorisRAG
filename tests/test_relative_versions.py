"""相对版本追问必须改变实际检索范围；不依赖模型生成措辞。"""
import json
import tempfile
import unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from doris_rag import telemetry, web
from doris_rag.pipeline import run_query
from doris_rag.query_understanding import understand_query
from doris_rag.sessions import SessionStore, prepare_turn, finish_state
from test_query_pipeline import fake_retriever, row

QUESTION = '列转行和聚合多维分析有什么区别，在什么场景下用'
FOLLOWUP = '那在其他版本找一下是否有列转行的相关信息'


def initial(versions=('4.x',)):
    turn = prepare_turn({}, QUESTION)
    return finish_state(turn['state'], {'status': 'generated', 'answer': 'fixture',
        'searched_versions': list(versions), 'understanding': understand_query(QUESTION)})


def plan(turn):
    return understand_query(turn['query'], turn['version'], turn['analysis_seed'], version_scope=turn['version_scope'], resolved_scope=turn.get('resolved_scope'), task=turn.get('task'))


class RelativeVersionsTests(unittest.TestCase):
    def test_focus_scope_and_raw_input_integrity(self):
        turn = prepare_turn(initial(), FOLLOWUP)
        self.assertEqual(turn['kind'], 'follow_up')
        self.assertEqual(plan(turn)['versions'], ['3.x', '2.1'])
        self.assertIn('列转行', turn['query'])
        self.assertNotIn('聚合多维分析', turn['query'])
        self.assertEqual(turn['state']['raw_inputs'], [QUESTION, FOLLOWUP])
        self.assertIsNone(turn['analysis_seed'])
        self.assertIsNone(turn['state']['environment_version'])

    def test_actual_retrieval_scope(self):
        turn = prepare_turn(initial(), FOLLOWUP)
        engine = fake_retriever([row('列转行定义与适用条件。', v, n) for n, v in enumerate(('4.x', '3.x', '2.1'))])
        with telemetry.request_trace(True, persist=False) as trace, patch(
                'doris_rag.query_understanding.chat_completion', side_effect=AssertionError('no model')), patch(
                'doris_rag.pipeline.enrich_context', side_effect=lambda rows, query: [{**r, 'contexts': [], 'context_status': 'complete'} for r in rows]):
            result = run_query(turn['query'], turn['version'], version_scope=turn['version_scope'], resolved_scope=turn.get('resolved_scope'), task=turn.get('task'),
                               retriever=engine, mode='dense', evidence_only=True)
        self.assertEqual(result['status'], 'evidence_only')
        self.assertEqual([e['version'] for e in trace.data['events'] if e['name']=='retrieval.scope_filter'], ['3.x', '2.1'])
        self.assertEqual({r['source_version'] for r in result['evidence']}, {'3.x', '2.1'})

    def test_relative_complement_and_missing_scope(self):
        for previous, expected in [(('3.x',), ['4.x','2.1']), (('3.x','2.1'), ['4.x'])]:
            self.assertEqual(plan(prepare_turn(initial(previous), FOLLOWUP))['versions'], expected)
        self.assertIn('没有其他', prepare_turn(initial(('4.x','3.x','2.1')), FOLLOWUP)['clarification'])
        self.assertIn('无法确认', prepare_turn(prepare_turn({}, QUESTION)['state'], FOLLOWUP)['clarification'])

    def test_all_explicit_and_ordinary_followup(self):
        state = initial()
        for text, expected in [('所有版本都查一下', ['4.x','3.x','2.1']), ('那在3.x查一下', ['3.x']),
                               ('那具体怎么用？', ['4.x'])]:
            self.assertEqual(plan(prepare_turn(state, text))['versions'], expected)
        state = prepare_turn({}, 'Doris 3.0.2 的 PI() 返回什么？')['state']
        self.assertEqual(plan(prepare_turn(state, '那具体呢？'))['versions'], ['3.x'])

    def test_no_explicit_focus_keeps_subject_and_next_relative_uses_actual_scope(self):
        turn = prepare_turn(initial(), '那其他版本呢？')
        self.assertIn(QUESTION, turn['query'])
        updated = finish_state(turn['state'], {'status':'execution_failed','answer':'fixture', 'searched_versions':['3.x','2.1']})
        self.assertEqual(plan(prepare_turn(updated, FOLLOWUP))['versions'], ['4.x'])

    def test_generation_failure_preserves_executed_scope(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(telemetry, 'TRACE_DIR', Path(tmp)):
            db = SessionStore(Path(tmp)/'sessions.sqlite3')
            session = db.create()
            _, mid, turn = db.accept(session['id'], QUESTION)
            def failed(*args, **kwargs):
                telemetry.event('retrieval.scope_filter', version='4.x')
                raise TimeoutError('synthetic generation failure')
            with patch.object(web, 'index_ready', return_value=True), patch.object(web, 'run_query', side_effect=failed):
                web.complete_turn(db, session['id'], mid, turn, perf_counter(), 0)
            saved = db.get(session['id'])
            self.assertEqual(saved['messages'][-1]['result']['searched_versions'], ['4.x'])
            self.assertEqual(plan(db.accept(session['id'], FOLLOWUP)[2])['versions'], ['3.x','2.1'])

    def test_legacy_failed_turn_recovers_only_saved_trace_scope(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(telemetry, 'TRACE_DIR', Path(tmp)):
            trace = Path(tmp)/'legacy.json'
            trace.write_text(json.dumps({'events':[{'name':'retrieval.scope_filter','version':'4.x'}]}), encoding='utf-8')
            db = SessionStore(Path(tmp)/'sessions.sqlite3')
            session = db.create()
            _, mid, turn = db.accept(session['id'], QUESTION)
            db.finish(session['id'], mid, {'status':'execution_failed', 'answer':'fixture', 'diagnostics':{'trace_path':str(trace)}})
            restored = db.accept(session['id'], FOLLOWUP)[2]
            self.assertEqual(plan(restored)['versions'], ['3.x','2.1'])
            self.assertNotIn('聚合多维分析', restored['query'])
