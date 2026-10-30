"""增量落盘、异常恢复与计时口径；无网络或模型调用。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doris_rag import telemetry


class TracePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        p = patch.object(telemetry, 'TRACE_DIR', self.directory)
        p.start()
        self.addCleanup(p.stop)

    def test_events_append_and_final_json_remains_compatible(self):
        with telemetry.request_trace(True) as trace:
            for i in range(20):
                trace.event('candidate', {'rank': i, 'chunk_text': '正文' * 100})
            self.assertFalse(trace.path.exists())
            recovered = telemetry.load_trace(trace.path)
            self.assertEqual(recovered['events'], trace.data['events'])
            trace.finish({'status': 'generated', 'answer': 'result'})
            self.assertEqual(trace.snapshot_writes, 1)
            trace.save(final=True)
            self.assertEqual(trace.snapshot_writes, 1)
        direct = json.loads(trace.path.read_text(encoding='utf-8'))
        journal = telemetry.load_trace(trace.journal_path)
        for key in ('events', 'stages', 'model_calls', 'result'):
            self.assertEqual(direct[key], journal[key])

    def test_actual_process_exit_keeps_candidates_and_pre_send_request(self):
        # os._exit不运行finally，用真实子进程验证非正常中断的落盘边界。
        script = '''
import os, sys
from pathlib import Path
from doris_rag import telemetry as t
t.TRACE_DIR = Path(sys.argv[1])
with t.request_trace(True) as trace:
    trace.secrets.append('fixture-private-key')
    with t.stage('retrieval.search'):
        t.event('retrieval.raw_dense', [{'chunk_id':'needed', 'chunk_text':'保留的原文'}])
    with t.stage('generation.answer'):
        t.model_post(lambda *a, **kw: os._exit(0), 'https://fixture.invalid',
            secret='fixture-private-key', json={'model':'fixture', 'messages':[{'role':'user','content':'真实送模正文'}]})
'''
        subprocess.run([sys.executable, '-c', script, str(self.directory)], check=True, timeout=30)
        journal = next(self.directory.glob('*.jsonl'))
        self.assertFalse(journal.with_suffix('.json').exists())
        result = telemetry.load_trace(journal.with_suffix('.json'))
        self.assertEqual(result['last_completed_stage'], 'retrieval.search')
        self.assertEqual(result['events'][0]['data'][0]['chunk_id'], 'needed')
        self.assertEqual(result['model_calls'][0]['request']['messages'][0]['content'], '真实送模正文')
        self.assertEqual(result['model_calls'][0]['status'], 'started')
        self.assertEqual(result['stages'][-1]['status'], 'started')
        self.assertNotIn('fixture-private-key', journal.read_text(encoding='utf-8'))

    def test_recovery_does_not_hide_commit_progress_behind_earlier_snapshot(self):
        with telemetry.request_trace(True) as trace:
            trace.finish({'status': 'generated', 'answer': 'prior snapshot'})
            with telemetry.stage('delivery.session_commit'):
                trace.event('commit-progress', {'n': 1})
                latest = telemetry.load_trace(trace.path)
                self.assertEqual(latest['events'][-1]['name'], 'commit-progress')
                self.assertEqual(latest['stages'][-1]['status'], 'started')
                self.assertTrue(latest['recovery']['snapshot_stale'])

    def test_truncated_tail_is_explicit_and_middle_corruption_is_rejected(self):
        trace = telemetry.RequestTrace(True)
        trace.save()
        trace.event('kept', {'n': 1})
        with trace.journal_path.open('ab') as stream:
            stream.write(b'{"seq":')
        recovered = telemetry.load_trace(trace.path)
        self.assertTrue(recovered['recovery']['incomplete_tail'])
        self.assertEqual(recovered['events'][0]['name'], 'kept')
        trace.journal_path.write_bytes(trace.journal_path.read_bytes() + b'\n')
        with self.assertRaises(json.JSONDecodeError):
            telemetry.load_trace(trace.path)

    def test_stage_subtracts_nested_recording_cost_with_fixed_clock(self):
        clock = [100.0]
        with telemetry.request_trace(True) as trace, patch.object(telemetry, 'perf_counter', side_effect=lambda: clock[0]):
            original = trace._write_record
            def write(value):
                clock[0] += 2.0
                original(value)
            with patch.object(trace, '_write_record', side_effect=write):
                with telemetry.stage('parent'):
                    clock[0] += 3.0
                    with telemetry.stage('child'):
                        trace.event('event', {'value': 1})
                        clock[0] += 5.0
            parent, child = trace.data['stages']
            self.assertEqual(parent['seconds'], 8.0)
            self.assertEqual(child['seconds'], 5.0)
            for s in (parent, child):
                self.assertEqual(s['wall_seconds'], s['seconds'] + s['logging_seconds'])
                self.assertGreater(s['logging_seconds'], 0)
            trace.finish({'status': 'fixture'})

    def test_model_elapsed_excludes_journal_saving(self):
        clock = [100.0]
        class Response:
            status_code = 200
            text = '响应'
        with telemetry.request_trace(True) as trace, patch.object(telemetry, 'perf_counter', side_effect=lambda: clock[0]):
            original = trace._write_record
            def write(value):
                clock[0] += 2.0
                original(value)
            def send(*a, **kw):
                clock[0] += 7.0
                return Response()
            with patch.object(trace, '_write_record', side_effect=write):
                telemetry.model_post(send, 'https://fixture.invalid', json={'model': 'fixture', 'messages': []})
            self.assertEqual(trace.data['model_calls'][0]['seconds'], 7.0)
            trace.finish({'status': 'fixture'})

    def test_metadata_mode_journal_omits_full_bodies(self):
        with telemetry.request_trace(False) as trace:
            trace.event('candidate', {'chunk_text': 'unique-body-secret'}, count=1)
            trace.finish({'status': 'generated', 'answer': 'unique-answer-secret'})
        for path in (trace.path, trace.journal_path):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('unique-body-secret', text)
            self.assertNotIn('unique-answer-secret', text)

    def test_append_failure_preserves_original_exception(self):
        with self.assertRaisesRegex(ValueError, 'business-failure'):
            with telemetry.request_trace(True) as trace:
                with patch.object(trace, '_write_record', side_effect=OSError('disk-failure')):
                    with telemetry.stage('retrieval.search'):
                        raise ValueError('business-failure')
        result = telemetry.load_trace(trace.path)
        self.assertEqual(result['failure']['error_type'], 'ValueError')
        self.assertEqual(result['recording_error'], 'OSError')

    def test_transport_interrupt_is_not_replaced_by_timing_error(self):
        def stop(*args, **kwargs):
            raise KeyboardInterrupt()
        with telemetry.request_trace(True) as trace:
            with self.assertRaises(KeyboardInterrupt):
                telemetry.model_post(stop, 'https://fixture.invalid', json={'model': 'fixture', 'messages': []})
            self.assertGreaterEqual(trace.data['model_calls'][0]['seconds'], 0)


if __name__ == '__main__':
    unittest.main()
