"""Eval执行连接的无网络验证；不把模拟回答当作质量评测结果。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from doris_rag import evaluation as ev

BASE = ev.ROOT / "reports/quality_baseline_20261025"


class ExecutionTests(unittest.TestCase):
    def test_capture_preserves_response_redacts_and_enforces_budget(self):
        with tempfile.TemporaryDirectory(dir=ev.ROOT / "reports") as temp:
            c = ev._CurrentCapture({"api_key": "test-secret", "base_url": "https://model.invalid"}, Path(temp), 1)
            c.trace = {"case_id": "one", "model_calls": []}
            response = SimpleNamespace(status_code=200, text='{"answer":"test-secret"}')
            original = Mock(return_value=response)
            self.assertIs(c.post(original, "https://model.invalid/chat/completions", json={"messages": []}), response)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                c.post(original, "https://model.invalid/chat/completions", json={})
            self.assertEqual(original.call_count, 1)
            self.assertEqual(c.network_calls, 1)
            self.assertNotIn("test-secret", (Path(temp) / "traces/one.json").read_text())

    def test_provider_circuit_stops_after_three_failures(self):
        with tempfile.TemporaryDirectory(dir=ev.ROOT / "reports") as temp:
            c = ev._CurrentCapture({"api_key": "secret", "base_url": "https://model.invalid"}, Path(temp), 40)
            c.trace = {"case_id": "one", "model_calls": []}
            original = Mock(return_value=SimpleNamespace(status_code=500, text="{}"))
            for _ in range(3):
                c.post(original, "https://model.invalid/chat/completions", json={})
            with self.assertRaises(RuntimeError):
                c.post(original, "https://model.invalid/chat/completions", json={})
            self.assertEqual(original.call_count, 3)

    def test_invalid_task_labels_are_not_converted_to_failure_score(self):
        result = ev.semantic_from_labels({"task_requirements": [{"id": "same", "passed": False}, {"id": "same", "passed": True}]})
        self.assertEqual(result["task_completion"]["status"], "invalid_data")
        self.assertIsNone(result["task_completion"]["value"])

    def test_runner_persists_all_cases_and_compatible_reuse_calls_no_model(self):
        cfg = {"api_key": "test-secret", "base_url": "https://model.invalid", "model": "test"}
        fake = SimpleNamespace(rows=[], manifest={"build_id": "fixture"}, _rank=lambda *a, **k: [], search_plan=lambda *a, **k: [])
        def result(*args, **kwargs):
            from doris_rag.telemetry import current_trace
            self.assertFalse(current_trace.get().persist)
            return {"status": "needs_clarification", "answer": "test fixture", "evidence": [],
                    "understanding": {"status": "needs_clarification"}, "timings": {"llm_calls": 0, "total_seconds": 0}}
        with tempfile.TemporaryDirectory(dir=ev.ROOT / "reports") as temp, \
                patch("doris_rag.generation.llm_config", return_value=cfg), \
                patch("doris_rag.generation.requests.post", side_effect=AssertionError("no network")), \
                contextlib.redirect_stdout(io.StringIO()):
            a, b = Path(temp) / "a", Path(temp) / "b"
            first = ev.run_current(BASE, a, _retriever_factory=lambda: fake, _query_runner=result)
            second = ev.run_current(BASE, b, reuse_current=a, _retriever_factory=Mock(side_effect=AssertionError("no index load")))
            self.assertEqual(first["recorded"], 35)
            self.assertEqual(second["reused_compatible_cases"], 35)
            self.assertEqual(second["network_model_calls"], 0)
            self.assertEqual(second["provider_usage"]["usage_recorded_calls"], 0)
            for r in ev.read_jsonl(a / "cases.jsonl"):
                self.assertEqual(r["scores"]["quality_status"], "pending_review")
                self.assertIsNone(r["scores"]["semantic"]["task_completion"]["value"])
                self.assertTrue((a / r["trace_path"]).exists())
            c, d = Path(temp) / "scored_a", Path(temp) / "scored_b"
            ev.rescore_current(a, c)
            ev.rescore_current(a, d)
            self.assertEqual((c / "cases.jsonl").read_bytes(), (d / "cases.jsonl").read_bytes())
            self.assertTrue(ev.compare_current(c, d)["comparable"])
            # 以下仅为模拟答案的合成审核夹具，不用于真实35题的质量分数。
            item = ev.read_jsonl(a / "review_queue.jsonl")[0]
            item.update(reviewer="fixture-reviewer", decomposition_exhaustiveness_human_confirmed=True, reference_reviewed=True,
                labels={"answer_claims": [{"id": "c1", "correct": True, "supported": False}],
                "reference_points": [{"id": "r1", "covered": True}], "task_requirements": [{"id": "t1", "passed": False}]})
            annotations = Path(temp) / "fixture_annotations.jsonl"
            ev.write_jsonl(annotations, [item])
            graded = Path(temp) / "graded"
            ev.rescore_current(a, graded, annotations)
            first_record = ev.read_jsonl(graded / "cases.jsonl")[0]
            self.assertEqual(first_record["scores"]["semantic"]["factual_correctness_f1"]["value"], 1)
            self.assertEqual(first_record["scores"]["semantic"]["faithfulness"]["value"], 0)
            self.assertEqual(first_record["scores"]["quality_status"], "failed_by_provided_review")


if __name__ == "__main__":
    unittest.main()
