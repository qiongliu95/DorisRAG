"""评测规则回归：有效证据不误杀，缺日志/未知语义不判通过。"""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from doris_rag import evaluation as ev
from doris_rag import evaluation_semantics as sem

BASE = ev.ROOT / "reports/quality_baseline_20261025"


def row(text="值5", path="a.md", version="4.x", **kwargs):
    return {"rank": 1, "chunk_id": path, "chunk_text": text, "source_version": version, "relative_path": path, **kwargs}


def group(path="a.md", terms=None, version="4.x"):
    return {"id": path, "alternatives": [[{"version": version, "path": path, "terms": terms or ["值5"]}]]}


class EvaluationRulesTests(unittest.TestCase):
    def test_equivalent_sources_same_fact_not_same_filename(self):
        g = group()
        g["alternatives"].append([{"version": "4.x", "path": "b.md", "terms": ["值5"]}])
        self.assertEqual(ev.score_groups([row(path="b.md")], [g])["value"], 1)
        self.assertEqual(ev.score_groups([row(path="b.md", version="3.x")], [g])["value"], 0)

    def test_conflicting_sources_are_required_not_alternatives(self):
        groups = [group("fe.md", ["5"], "3.x"), group("routine.md", ["1024"], "3.x")]
        partial = ev.score_groups([row("5", "fe.md", "3.x")], groups)
        self.assertEqual(partial["value"], .5)
        self.assertFalse(partial["all_hit"])
        full = ev.score_groups([row("5", "fe.md", "3.x"), row("1024", "routine.md", "3.x")], groups)
        self.assertTrue(full["all_hit"])

    def test_alternative_can_require_multiple_distinct_sources(self):
        g = group()
        g["alternatives"] = [[{"version": "4.x", "path": "a.md", "terms": ["A"]},
                              {"version": "4.x", "path": "b.md", "terms": ["B"]}]]
        self.assertEqual(ev.score_groups([row("A")], [g])["value"], 0)
        score = ev.score_groups([row("A"), row("B", "b.md")], [g])
        self.assertEqual(score["target_ranks"], [2])

    def test_raw_and_enriched_evidence_are_separate(self):
        r = row("引言", contexts=[{"text": "值5"}])
        self.assertEqual(ev.score_groups([r], [group()])["value"], 0)
        self.assertEqual(ev.score_groups([r], [group()], True)["value"], 1)

    def test_exact_identifier_value_and_version_boundaries(self):
        for term, wrong in [("5", "50"), ("默认值：5", "默认值：50"), ("V1", "V12"),
                            ("3.1.0", "3.1.0.1"), ("max_be", "max_be_extra")]:
            self.assertFalse(ev.matches_v2(row(wrong), {"version": "4.x", "path": "a.md", "terms": [term]}, False))
        self.assertTrue(ev.matches_v2(row("已有 `V1` 表"), {"version": "4.x", "path": "a.md", "terms": ["已有 V1 表"]}, False))

    def test_unknown_not_empty_recall_and_no_gold_not_applicable(self):
        self.assertIsNone(ev.score_groups(None, [group()])["value"])
        self.assertEqual(ev.score_groups([], [group()])["value"], 0)
        self.assertEqual(ev.score_groups(None, [])["status"], "not_applicable")
        self.assertEqual(ev.score_groups([], [{"id": "x", "alternatives": []}])["status"], "invalid_data")

    def test_context_precision_uses_complete_labels_and_order(self):
        rows = [row(path="noise"), row(path="core")]
        self.assertIsNone(ev.context_precision(rows, {"core": True})["value"])
        result = ev.context_precision(rows, {"core": True, "noise": False})
        self.assertEqual(result["value"], .5)
        self.assertEqual(result["precision"], .5)
        self.assertEqual(ev.context_precision(rows, {"core": False, "noise": False})["value"], 0)

    def test_partial_labels_do_not_become_full_semantic_score(self):
        labels = {"answer_claims": [{"id": "a", "correct": True, "supported": True}, {"id": "b", "correct": None}]}
        score = ev.semantic_from_labels(labels, decomposition_exhaustiveness_human_confirmed=True)
        self.assertIsNone(score["factual_precision"]["value"])
        self.assertEqual(score["factual_precision"]["status"], "partially_evaluated")
        self.assertIsNone(score["faithfulness"]["value"])
        duplicate = ev.fraction_labels([{"id": "a", "x": True}, {"id": "a", "x": True}], "x", "test")
        self.assertEqual(duplicate["status"], "invalid_data")

    def test_known_failed_requirement_remains_failure_with_other_unknowns(self):
        result = ev.semantic_from_labels({"task_requirements": [{"id": "a", "passed": False}, {"id": "b", "passed": None}]})
        self.assertEqual(result["task_completion"]["value"], 0)
        self.assertIsNone(result["factual_precision"]["value"])

    def test_partial_aggregate_does_not_claim_full_pass(self):
        result = ev.aggregate_metrics([ev.metric(1, "evaluated"), ev.metric(reason="missing")])
        self.assertIsNone(result["value"])
        self.assertEqual(result["evaluated_subset_mean"], 1)
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["missing"], 1)

    def test_review_labels_bound_to_answer_context_and_rule_version(self):
        sample = {"question": "q", "response": "a", "contexts": ["c"], "versions": ["4.x"]}
        annotation = {"reviewer": "unit-review", "subject_sha256": ev.review_subject(sample, "rules1"),
                      "decomposition_exhaustiveness_human_confirmed": True,
                      "labels": {"answer_claims": [{"id": "a", "correct": True, "supported": False}]}}
        score = ev.score_reviewed_labels(sample, annotation, "rules1")
        self.assertEqual(score["metrics"]["factual_precision"]["value"], 1)
        self.assertEqual(score["metrics"]["faithfulness"]["value"], 0)
        for changed, rules in (({**sample, "response": "b"}, "rules1"), ({**sample, "contexts": ["other"]}, "rules1"), (sample, "rules2")):
            score = ev.score_reviewed_labels(changed, annotation, rules)
            self.assertTrue(all(m["status"] == "invalid_data" for m in score["metrics"].values()))

    def test_complete_labels_require_confirmed_denominator_scopes_independently(self):
        # 只有合成夹具可以把全集审核设为true，真实部分确认文件保持原样。
        sample = {"response": "fixture", "retrieved_contexts": ["fixture evidence"]}
        labels = {"answer_claims": [{"id": "a", "correct": True, "supported": False},
                                    {"id": "b", "correct": False, "supported": True}],
                  "reference_points": [{"id": "r1", "covered": True}, {"id": "r2", "covered": False}]}
        for claims_scope, reference_scope in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(claims_scope=claims_scope, reference_scope=reference_scope):
                annotation = {"reviewer": "fixture-reviewer", "labels": labels,
                    "subject_sha256": ev.review_subject(sample, "rules"),
                    "decomposition_exhaustiveness_human_confirmed": claims_scope,
                    "reference_reviewed": reference_scope}
                result = ev.score_reviewed_labels(sample, annotation, "rules")["metrics"]
                for name in ("factual_precision", "faithfulness"):
                    if claims_scope:
                        self.assertEqual(result[name]["value"], .5)
                        self.assertEqual(result[name]["denominator"], 2)
                    else:
                        self.assertIsNone(result[name]["value"])
                        self.assertEqual(result[name]["scope_status"], "unreviewed")
                        self.assertEqual(result[name]["reviewed"], 2)
                        self.assertEqual(result[name]["confirmed_true"], 1)
                        self.assertIsNone(result[name]["denominator"])
                self.assertEqual(result["answer_completeness"]["value"], .5 if reference_scope else None)
                self.assertEqual(result["factual_correctness_f1"]["value"], .5 if claims_scope and reference_scope else None)

    def test_confirmed_scope_still_requires_each_metric_labels(self):
        labels = {"answer_claims": [{"id": "a", "correct": True, "supported": None}],
                  "reference_points": [{"id": "r", "covered": True}],
                  "task_requirements": [{"id": "t", "passed": True}, {"id": "unknown", "passed": None}]}
        result = ev.semantic_from_labels(labels, decomposition_exhaustiveness_human_confirmed=True, reference_reviewed=True)
        self.assertEqual(result["factual_precision"]["value"], 1)
        self.assertEqual(result["answer_completeness"]["value"], 1)
        self.assertIsNone(result["faithfulness"]["value"])
        self.assertIsNone(result["task_completion"]["value"])

    def test_unconfirmed_scope_does_not_hide_invalid_ids_or_accept_truthy_flags(self):
        invalid = ev.semantic_from_labels({"answer_claims": [{"id": "a", "correct": True}, {"id": "a", "correct": True}]})
        self.assertEqual(invalid["factual_precision"]["status"], "invalid_data")
        for flag in (None, False, 1, "true"):
            with self.subTest(flag=flag):
                result = ev.semantic_from_labels({"answer_claims": [{"id": "a", "correct": True}]},
                                                 decomposition_exhaustiveness_human_confirmed=flag)
                self.assertIsNone(result["factual_precision"]["value"])

    def test_four_real_partial_confirmations_keep_scopes_and_labels(self):
        path = ev.ROOT / "reports/partial_confirmation_20261025/validated_annotations.jsonl"
        original = path.read_bytes()
        annotations = ev.read_jsonl(path)
        fields = [item[field] for annotation in annotations for items in annotation["labels"].values()
                  for item in items for field in ("correct", "supported", "covered", "passed", "satisfied") if field in item]
        self.assertEqual(sum(type(x) is bool for x in fields), 24)
        self.assertEqual(sum(x is None for x in fields), 42)
        result = {a["id"]: ev.score_reviewed_labels(a["sample"], a, a["binding"]["rules_sha256"])["metrics"] for a in annotations}
        for cid, metrics in result.items():
            self.assertIsNone(metrics["factual_precision"]["value"])
            self.assertIsNone(metrics["faithfulness"]["value"])
            self.assertIsNone(metrics["answer_completeness"]["value"])
            self.assertIsNone(metrics["factual_correctness_f1"]["value"])
            self.assertEqual(metrics["task_completion"]["value"], 0 if cid in ("compare_rl", "ragas_Q19") else None)
        self.assertEqual(result["upload_removed"]["faithfulness"]["confirmed_true"], 2)
        self.assertEqual(result["upload_removed"]["faithfulness"]["confirmed_false"], 1)
        self.assertEqual(result["compare_rl"]["answer_completeness"]["reviewed"], 3)
        self.assertEqual(result["ragas_Q19"]["task_completion"]["reviewed"], 1)
        self.assertEqual(result["ragas_Q19"]["task_completion"]["total"], 3)
        self.assertEqual(path.read_bytes(), original)

    def test_model_default_off_even_with_injected_object(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden")):
            # 默认关闭分支应在任何await之前返回，不需创建Windows本地套接字事件循环。
            coroutine = sem.score_semantics({"response": "answer"}, llm=object())
            with self.assertRaises(StopIteration) as stopped:
                coroutine.send(None)
            result = stopped.exception.value
        self.assertTrue(all(m["value"] is None for m in result["metrics"].values()))
        with self.assertRaises(PermissionError):
            sem.configured_judge()

    def test_reference_requires_review_and_matching_version_scope(self):
        sample = {"response": "a", "reference": "r", "reference_reviewed": False, "reference_applicability_reviewed": True,
                  "source_versions": ["4.x"], "reference_versions": ["4.x"]}
        self.assertFalse(sem.readiness(sample)["reviewed_reference_in_scope"])
        sample["reference_reviewed"] = True
        self.assertTrue(sem.readiness(sample)["reviewed_reference_in_scope"])
        sample["reference_versions"] = ["3.x"]
        self.assertFalse(sem.readiness(sample)["reviewed_reference_in_scope"])
        self.assertIsNone(sem.numeric_metric(float("nan"), "test")["value"])
        self.assertEqual(sem.numeric_metric(-.2, "cosine", minimum=-1)["value"], -.2)
        self.assertIsNone(sem.numeric_metric(-.2, "fraction")["value"])

    def test_standard_claim_coverage_not_native_recall_shortcut(self):
        scores = sem.claim_scores(["A", "B"], ["X", "Y", "Z"], [1, 1], [1, 0, 0])
        self.assertEqual(scores["factual_precision"], 1)
        self.assertAlmostEqual(scores["answer_completeness"], 1 / 3)
        self.assertAlmostEqual(scores["factual_correctness_f1"], .5)
        for args in (([], ["X"], [], [1]), (["A"], ["X"], [], [1]), (["A"], ["X"], [2], [1])):
            with self.assertRaises(ValueError):
                sem.claim_scores(*args)

    def test_existing_model_transport_is_bounded_traced_and_redacted(self):
        # 只测试已有模型接口的传输/预算保护；这些类型替身不当作Ragas原生集成验证。
        fake = {"ragas.llms.base": types.SimpleNamespace(BaseRagasLLM=object),
                "langchain_core.outputs": types.SimpleNamespace(Generation=lambda **k: types.SimpleNamespace(**k),
                                                                LLMResult=lambda **k: types.SimpleNamespace(**k))}
        cfg = {"api_key": "unit-secret", "base_url": "https://model.invalid/v1", "model": "test"}
        data = {"choices": [{"finish_reason": "stop", "message": {"content": "unit-secret answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        response = Mock(text=json.dumps(data), status_code=200)
        response.json.return_value = data
        with tempfile.TemporaryDirectory(dir=ev.ROOT / "reports") as temp, patch.dict("sys.modules", fake), \
                patch("doris_rag.generation.llm_config", return_value=cfg), \
                patch("doris_rag.generation.requests.post", return_value=response) as transport:
            path = Path(temp) / "judge.jsonl"
            judge = sem.configured_judge(authorization={"approved": True, "cost_reported": True}, max_calls=2, trace_path=path)
            prompt = types.SimpleNamespace(to_string=lambda: "test prompt")
            judge.generate_text(prompt)
            judge.generate_text(prompt)
            with self.assertRaisesRegex(RuntimeError, "budget"):
                judge.generate_text(prompt)
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(len(ev.read_jsonl(path)), 2)
            text = path.read_text(encoding="utf8")
            self.assertNotIn("unit-secret", text)
            self.assertIn("raw_response", text)
            self.assertIn("actual_request", text)
            self.assertNotIn("Authorization", text)

    @unittest.skipIf(importlib.util.find_spec("ragas"), "仅验证缺依赖分支")
    def test_missing_ragas_returns_unassessed_without_model_call(self):
        coro = sem.score_semantics({"response": "a"}, llm=object(), allow_model=True)
        with self.assertRaises(StopIteration) as stopped:
            coro.send(None)
        self.assertTrue(all(m["value"] is None for m in stopped.exception.value["metrics"].values()))
        self.assertTrue(all("Ragas" in m["reason"] for m in stopped.exception.value["metrics"].values()))


class FrozenReplayTests(unittest.TestCase):
    def test_frozen_trace_parses_actual_contexts_not_citation_subset(self):
        p = BASE / "historical_inputs/reports/qa_fix_20261025/round2"
        records = ev.read_jsonl(p / "cases.jsonl")
        a = next(r for r in records if r["case"] == "A")
        trace = ev.read_json(p / "traces" / (a["message_id"] + ".json"))
        contexts = ev.sent_contexts(trace)
        self.assertEqual(len(contexts), 5)
        self.assertEqual(len(a["result"]["evidence"]), 4)
        forged = copy.deepcopy(trace)
        for call in forged["model_calls"]:
            for message in call["request"]["messages"]:
                message["content"] = message["content"].replace("\n链接：", "\n缺少链接：", 1)
        self.assertIsNone(ev.sent_contexts(forged))

    def test_frozen_replay_repeatable_and_distinguishes_failures(self):
        with tempfile.TemporaryDirectory(dir=ev.ROOT / "reports") as temp:
            a, b = Path(temp) / "a", Path(temp) / "b"
            with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
                result = ev.replay(BASE, a)
                ev.replay(BASE, b)
            for file in ("manifest.json", "summary.json", "offline_cases.jsonl", "historical_cases.jsonl"):
                self.assertEqual((a / file).read_bytes(), (b / file).read_bytes())
            self.assertEqual(result["corrected_rule_false_negatives"], ["index_v1", "stream_newkey"])
            self.assertIsNone(result["pipeline_all_groups_hit"]["value"])
            self.assertEqual(result["pipeline_all_groups_hit"]["missing"], 1)
            rows = {r["id"]: r for r in ev.read_jsonl(a / "offline_cases.jsonl")}
            self.assertEqual(rows["compare_rl"]["attribution"], "understanding_blocked")
            self.assertEqual(rows["compare_rl"]["pipeline_evidence_delivery"]["value"], 0)
            history = {r["id"]: r for r in ev.read_jsonl(a / "historical_cases.jsonl")}
            for case in ("A09_regression", "A16_regression_only"):
                r = history["round1_actual/" + case]
                self.assertIn("ranking_or_selection", r["attribution"])
                self.assertEqual(r["acceptance_verdict"], "fail")
                self.assertIsNone(r["semantic"]["faithfulness"]["value"])
            self.assertTrue(ev.compare_scores(a, b)["comparable"])
            manifest = ev.read_json(b / "manifest.json")
            manifest["scoring_version"] = "other"
            (b / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
            self.assertFalse(ev.compare_scores(a, b)["comparable"])
            with self.assertRaises(FileExistsError):
                ev.replay(BASE, a)

    def test_rule_does_not_silently_apply_to_a_new_question(self):
        rules = ev.read_json(ev.ROOT / "eval/scoring_rules_v2.json")
        with self.assertRaises(ValueError):
            ev.evidence_groups({"id": "index_v1", "question": "different", "gold": []}, rules)
        for cid in ("rl_be3", "compare_rl", "A02", "A03"):
            self.assertTrue(any("冲突" in item for item in ev.task_rubric(cid, rules)))


if __name__ == "__main__":
    unittest.main()
