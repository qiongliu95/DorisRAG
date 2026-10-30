"""验证验收旁路记录不改变请求、异常或泄漏认证信息。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("acceptance_trace", Path(__file__).with_name("run_v1_acceptance_trace.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AcceptanceTraceTests(unittest.TestCase):
    def test_CONV006_registered_in_existing_live_entry_without_gold_in_tasks(self):
        tasks=audit.conversation_tasks('CONV006')
        self.assertEqual(len(tasks),6)
        self.assertEqual({t['session_key'] for t in tasks},{'CONV006'})
        self.assertEqual(tasks[1]['question'],'一般在什么场景下使用？')
        self.assertEqual(tasks[-1]['question'],'其他版本呢？')
        self.assertTrue(all('expected' not in t and 'gold' not in t for t in tasks))
        suite=audit.conversation_tasks('CONV006',True)
        self.assertEqual(len(suite),18)
        self.assertEqual(sum(t.get('session_key')=='D02-A18' for t in suite),8)

    def test_complete_raw_response_and_context_survive_without_headers(self):
        class Response:
            status_code = 200
            text = '{"choices":[{"finish_reason":"length","message":{"content":"raw-secret"}}]}'
        seen = []
        response = Response()
        def original(url, **kwargs):
            seen.append(kwargs)
            return response
        with tempfile.TemporaryDirectory(dir=audit.ROOT / "reports") as directory:
            active = {"message_id": "turn", "model_calls": []}
            token = audit.ACTIVE.set(active)
            try:
                capture = audit.observe_post(original, Path(directory), lambda x: x.replace("secret", "REDACTED"))
                result = capture("https://model.example/chat/completions", json={"messages": [{"role": "user", "content": "actual context"}]}, headers={"Authorization": "Bearer secret"})
            finally:
                audit.ACTIVE.reset(token)
            self.assertIs(result, response)
            saved = json.loads((Path(directory) / "turn.json").read_text(encoding="utf-8"))
            self.assertIn('"finish_reason":"length"', saved["model_calls"][0]["raw_response"])
            self.assertEqual(saved["model_calls"][0]["request"]["messages"][0]["content"], "actual context")
            self.assertNotIn("secret", json.dumps(saved))
            self.assertEqual(seen[0]["headers"]["Authorization"], "Bearer secret")

    def test_request_error_is_recorded_and_raised(self):
        def fail(*args, **kwargs):
            raise TimeoutError("private provider detail")
        with tempfile.TemporaryDirectory(dir=audit.ROOT / "reports") as directory:
            token = audit.ACTIVE.set({"message_id": "failed", "model_calls": []})
            try:
                capture = audit.observe_post(fail, Path(directory), lambda x: x)
                with self.assertRaises(TimeoutError):
                    capture("https://model.example/chat/completions", json={"messages": []})
            finally:
                audit.ACTIVE.reset(token)
            saved = (Path(directory) / "failed.json").read_text(encoding="utf-8")
            self.assertIn("request_failed", saved)
            self.assertNotIn("private provider detail", saved)

    def test_outside_acceptance_is_not_captured(self):
        with tempfile.TemporaryDirectory(dir=audit.ROOT / "reports") as directory:
            capture = audit.observe_post(lambda *a, **k: "unchanged", Path(directory), lambda x: x)
            self.assertEqual(capture("https://model.example/chat/completions"), "unchanged")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
