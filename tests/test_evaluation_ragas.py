"""使用已安装Ragas和脚本化模型输出验证适配，不调用真实服务或加载Embedding。"""
import asyncio
import importlib.util
import json
import unittest
from unittest.mock import patch

from doris_rag.evaluation_semantics import score_semantics

HAS_RAGAS = importlib.util.find_spec("ragas") is not None
if HAS_RAGAS:
    from ragas.llms.base import BaseRagasLLM
    from langchain_core.outputs import Generation, LLMResult

    class ScriptedJudge(BaseRagasLLM):
        def __init__(self, values):
            super().__init__()
            self.values = iter(values)
            self.calls = 0

        def generate_text(self, prompt, n=1, **kwargs):
            if n != 1:
                raise AssertionError("unexpected n")
            self.calls += 1
            return LLMResult(generations=[[Generation(text=json.dumps(next(self.values)))]] )

        async def agenerate_text(self, prompt, n=1, **kwargs):
            return self.generate_text(prompt, n=n)

        async def generate(self, prompt, n=1, **kwargs):
            return self.generate_text(prompt, n=n)

        def is_finished(self, response):
            return True


SAMPLE = {"user_input": "question", "response": "answer", "retrieved_contexts": ["evidence A", "evidence B"],
          "contexts_complete": True, "reference": "reviewed reference", "reference_reviewed": True,
          "reference_applicability_reviewed": True, "expected_behavior": "answer",
          "source_versions": ["4.x"], "reference_versions": ["4.x"], "task_rubric": ["满足两个要求，不能遗漏"]}


def nli(values):
    return {"statements": [{"statement": text, "reason": "scripted test", "verdict": verdict} for text, verdict in values]}


@unittest.skipUnless(HAS_RAGAS, "需使用experiments/ragas_20261023/.venv；不自动安装依赖")
class RagasAdapterTests(unittest.TestCase):
    def run_scoring(self, outputs, only, sample=None, embeddings=None):
        judge = ScriptedJudge(outputs)
        # 只阻断HTTP；Windows事件循环自身需要本地socketpair，不将其误判为模型调用。
        with patch("requests.sessions.Session.request", side_effect=AssertionError("real HTTP forbidden")):
            result = asyncio.run(score_semantics(sample or SAMPLE, llm=judge, allow_model=True, only=only, embeddings=embeddings))
        return result["metrics"], judge.calls

    def test_claim_precision_and_reference_coverage_have_distinct_denominators(self):
        result, calls = self.run_scoring([
            {"claims": ["A", "B"]}, {"claims": ["X", "Y", "Z"]},
            nli([("A", 1), ("B", 1)]), nli([("X", 1), ("Y", 0), ("Z", 0)])],
            ["factual_precision", "answer_completeness", "factual_correctness_f1"])
        self.assertEqual(calls, 4)
        self.assertEqual(result["factual_precision"]["value"], 1)
        self.assertAlmostEqual(result["answer_completeness"]["value"], 1 / 3)
        self.assertAlmostEqual(result["factual_correctness_f1"]["value"], .5)

    def test_unreviewed_or_wrong_version_reference_does_not_call_model(self):
        for change in ({"reference_reviewed": False}, {"reference_versions": ["3.x"]}):
            result, calls = self.run_scoring([], ["answer_completeness"], {**SAMPLE, **change})
            self.assertEqual(calls, 0)
            self.assertIsNone(result["answer_completeness"]["value"])

    def test_faithfulness_uses_actual_contexts_and_rejects_empty_statements(self):
        result, calls = self.run_scoring([{"statements": ["A", "B"]}, nli([("A", 1), ("B", 0)])], ["faithfulness"])
        self.assertEqual(calls, 2)
        self.assertEqual(result["faithfulness"]["value"], .5)
        empty, calls = self.run_scoring([{"statements": []}], ["faithfulness"])
        self.assertEqual(calls, 1)
        self.assertEqual(empty["faithfulness"]["status"], "invalid_judge_output")
        missing, calls = self.run_scoring([], ["faithfulness"], {**SAMPLE, "contexts_complete": False})
        self.assertEqual(calls, 0)
        self.assertIsNone(missing["faithfulness"]["value"])

    def test_missing_or_fabricated_nli_statements_not_silently_passed(self):
        for bad in (nli([("other", 1)]), nli([("A", 2)])):
            result, _ = self.run_scoring([{"statements": ["A"]}, bad], ["faithfulness"])
            self.assertEqual(result["faithfulness"]["status"], "judge_error")
            self.assertIsNone(result["faithfulness"]["value"])

    def test_task_completion_uses_rubric_not_generated_status(self):
        result, calls = self.run_scoring([{"reason": "missing required step", "verdict": 0}], ["task_completion"])
        self.assertEqual(calls, 1)
        self.assertEqual(result["task_completion"]["value"], 0)
        result, calls = self.run_scoring([], ["task_completion"], {**SAMPLE, "task_rubric": None})
        self.assertEqual(calls, 0)
        self.assertIsNone(result["task_completion"]["value"])

    def test_context_precision_is_rank_sensitive_ap(self):
        result, calls = self.run_scoring([{"reason": "irrelevant", "verdict": 0}, {"reason": "relevant", "verdict": 1}], ["context_precision"])
        self.assertEqual(calls, 2)
        self.assertEqual(result["context_precision"]["value"], .5)

    def test_relevancy_requires_injected_embeddings(self):
        class LocalTestEmbedding:
            def embed_query(self, text):
                return [1.0, 0.0]

            def embed_documents(self, texts):
                return [[1.0, 0.0] for _ in texts]

        result, calls = self.run_scoring([], ["answer_relevancy"])
        self.assertEqual(calls, 0)
        self.assertIsNone(result["answer_relevancy"]["value"])
        result, calls = self.run_scoring([{"question": "question", "noncommittal": 0}], ["answer_relevancy"], embeddings=LocalTestEmbedding())
        self.assertEqual(calls, 1)
        self.assertEqual(result["answer_relevancy"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
