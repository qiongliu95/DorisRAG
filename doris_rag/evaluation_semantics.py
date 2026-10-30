"""评测专用Ragas接入。默认不运行模型；不改变产品提示或生成逻辑。"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import threading
from collections import Counter

from .evaluation import metric

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

METRICS = ("factual_precision", "answer_completeness", "factual_correctness_f1",
           "faithfulness", "task_completion", "context_precision", "answer_relevancy")


def readiness(sample):
    answer = isinstance(sample.get("response"), str) and bool(sample["response"].strip())
    contexts = sample.get("retrieved_contexts")
    contexts_ok = (sample.get("contexts_complete") is True and isinstance(contexts, list) and bool(contexts)
                   and all(isinstance(c, str) and c.strip() for c in contexts))
    versions = sample.get("source_versions")
    reference_versions = sample.get("reference_versions")
    reference = (sample.get("reference_reviewed") is True and isinstance(sample.get("reference"), str)
                 and sample.get("reference_applicability_reviewed") is True
                 and bool(sample["reference"].strip()) and bool(sample.get("source_versions"))
                 and isinstance(versions, list) and isinstance(reference_versions, list)
                 and set(reference_versions) == set(versions))
    return {"answer": answer, "actual_contexts": contexts_ok, "reviewed_reference_in_scope": reference,
            "task_rubric": isinstance(sample.get("task_rubric"), list) and bool(sample["task_rubric"])
                           and all(isinstance(c, str) and c.strip() for c in sample["task_rubric"])}


def numeric_metric(value, reason, minimum=0.0):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 1:
        return metric(status="invalid_judge_output", reason="Judge返回NaN、越界或非数值；不得默认通过")
    return metric(float(value), "evaluated", reason, method="llm_assisted_requires_calibration", range=[minimum, 1.0])


def claim_scores(answer_claims, reference_claims, correct, covered):
    """答案精确率和参考覆盖率各用自己的分母；空、缺项、越界不产出分数。"""
    if (not answer_claims or not reference_claims or len(correct) != len(answer_claims) or len(covered) != len(reference_claims)
            or any(v not in (0, 1) for v in list(correct) + list(covered))):
        raise ValueError("invalid_or_incomplete_claim_verdicts")
    precision, recall = sum(correct) / len(answer_claims), sum(covered) / len(reference_claims)
    return {"factual_precision": precision, "answer_completeness": recall,
            "factual_correctness_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


async def score_semantics(sample, *, llm=None, allow_model=False, embeddings=None, only=METRICS):
    """注入显式获准的Judge后才运行；参考、上下文不足时逐指标保留null。"""
    if any(name not in METRICS for name in only):
        raise ValueError("不支持的语义指标")
    ready = readiness(sample)
    def packed(values, **details):
        return {"readiness": ready, "metrics": values,
                "acceptance_verdict": "fail" if sample.get("known_critical_failures") else "not_determined", **details}
    result = {name: metric(reason="未授权模型评分" if not allow_model else "未提供Judge") for name in only}
    if not allow_model or llm is None:
        return packed(result)
    try:
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics._factual_correctness import FactualCorrectness
        from ragas.metrics._factual_correctness import ClaimDecompositionInput
        from ragas.metrics._faithfulness import Faithfulness, NLIStatementInput, StatementGeneratorInput
        from ragas.metrics._aspect_critic import AspectCritic
        from ragas.metrics._context_precision import LLMContextPrecisionWithReference, QAC
        from ragas.metrics._answer_relevance import ResponseRelevancy
    except ImportError:
        return packed({name: metric(reason="缺少Ragas依赖；需恢复锁定的实验环境，不自动安装") for name in only})
    if not ready["answer"]:
        return packed({name: metric(reason="没有可评分的原始答案") for name in only})
    try:
        row = SingleTurnSample(user_input=sample.get("user_input", ""), response=sample["response"],
                              retrieved_contexts=sample.get("retrieved_contexts"), reference=sample.get("reference"))
    except Exception:
        return packed({name: metric(status="invalid_data", reason="Ragas样本字段格式不正确") for name in only})

    async def verify(prompt, premise, claims):
        output = await prompt.generate(data=NLIStatementInput(context=premise, statements=claims), llm=llm, retries_left=0)
        if Counter(v.statement for v in output.statements) != Counter(claims) or any(v.verdict not in (0, 1) for v in output.statements):
            raise ValueError("NLI_statement_or_verdict_mismatch")
        return [v.verdict for v in output.statements]
    claims_requested = set(only) & {"factual_precision", "answer_completeness", "factual_correctness_f1"}
    if claims_requested:
        if not ready["reviewed_reference_in_scope"]:
            for name in claims_requested:
                result[name] = metric(reason="缺少经审核、同版本范围的参考答案；旧expected_keypoints不自动当完整参考")
        else:
            try:
                # 本地Ragas原生recall复用答案TP作分子，并非严格的参考要点覆盖率。
                # 复用其断言拆解/NLI，分别按答案断言数和参考断言数计算P/R，避免口径混淆。
                judge = FactualCorrectness(llm=llm)
                answer_claims = (await judge.claim_decomposition_prompt.generate(data=ClaimDecompositionInput(response=sample["response"]), llm=llm, retries_left=0)).claims
                reference_claims = (await judge.claim_decomposition_prompt.generate(data=ClaimDecompositionInput(response=sample["reference"]), llm=llm, retries_left=0)).claims
                if not answer_claims or not reference_claims:
                    raise ValueError("empty_claims")
                correct = await verify(judge.nli_prompt, sample["reference"], answer_claims)
                covered = await verify(judge.nli_prompt, sample["response"], reference_claims)
                if len(correct) != len(answer_claims) or len(covered) != len(reference_claims):
                    raise ValueError("claim_verdict_count_mismatch")
                scores = claim_scores(answer_claims, reference_claims, correct, covered)
                for name in claims_requested:
                    result[name] = numeric_metric(scores[name], "Ragas断言/NLI；标准P/R分母，不冒充原生FactualCorrectness算法")
                    result[name]["claim_counts"] = {"answer": len(answer_claims), "reference": len(reference_claims)}
            except Exception as error:
                for name in claims_requested:
                    result[name] = metric(status="judge_error", reason=type(error).__name__)
    candidates = {
        "faithfulness": (ready["actual_contexts"], lambda: Faithfulness(llm=llm), "缺少完整实际送模上下文"),
        "task_completion": (ready["task_rubric"] and ready["actual_contexts"], lambda: AspectCritic(name="task_completion", llm=llm,
            definition="所有必要条件均满足才判1；保留冲突与未知边界，不以引用存在或回答较长代替完成。条件：" + json.dumps(sample.get("task_rubric"), ensure_ascii=False),
            strictness=1, max_retries=0), "缺少明确任务判据或实际送模证据"),
        "context_precision": (ready["actual_contexts"] and ready["reviewed_reference_in_scope"], lambda: LLMContextPrecisionWithReference(llm=llm), "缺少完整有序上下文或审核参考答案"),
        "answer_relevancy": (embeddings is not None and sample.get("expected_behavior") == "answer",
            lambda: ResponseRelevancy(llm=llm, embeddings=embeddings, strictness=1), "缺少评测Embedding或未明确为可回答题；正确拒答/澄清不按普通答案相关性扣分")}
    for name, (eligible, factory, missing) in candidates.items():
        if name not in only:
            continue
        if not eligible:
            result[name] = metric(reason=missing)
            continue
        try:
            if name == "faithfulness":
                judge = factory()
                statements = (await judge.statement_generator_prompt.generate(
                    data=StatementGeneratorInput(question=sample.get("user_input", ""), answer=sample["response"]), llm=llm, retries_left=0)).statements
                if not statements:
                    result[name] = metric(status="invalid_judge_output", reason="未提取到断言，不能给Faithfulness=1")
                    continue
                verdicts = await verify(judge.nli_statements_prompt, "\n".join(sample["retrieved_contexts"]), statements)
                value = sum(verdicts) / len(statements)
            elif name == "context_precision":
                judge = factory()
                labels = []
                for context in sample["retrieved_contexts"]:
                    verdict = await judge.context_precision_prompt.generate(
                        data=QAC(question=sample.get("user_input", ""), context=context, answer=sample["reference"]), llm=llm, retries_left=0)
                    if verdict.verdict not in (0, 1):
                        raise ValueError("context_verdict_out_of_range")
                    labels.append(verdict.verdict)
                numerator, count = 0.0, 0
                for i, relevant in enumerate(labels, 1):
                    count += relevant
                    if relevant:
                        numerator += count / i
                value = numerator / count if count else 0.0
            else:
                value = await factory().single_turn_ascore(row)
            result[name] = numeric_metric(float(value), "Ragas辅助评分；不能覆盖人工已确认的关键失败", minimum=-1.0 if name == "answer_relevancy" else 0.0)
        except Exception as error:
            result[name] = metric(status="judge_error", reason=type(error).__name__)
    return packed(result, judge_profile=getattr(llm, "profile", {"type": type(llm).__name__}))


def configured_judge(*, authorization=None, max_calls=0, trace_path: Path | None = None):
    """复用现有模型接口；不提供CLI付费开关，调用者必须先取得明确授权。"""
    if not authorization or authorization.get("approved") is not True or authorization.get("cost_reported") is not True:
        raise PermissionError("未获付费调用授权；仅允许离线重放或注入本地测试Judge")
    if type(max_calls) is not int or max_calls <= 0 or trace_path is None:
        raise ValueError("需提供正整数调用上限及独立追踪文件")
    if trace_path.exists():
        raise FileExistsError("不覆盖已有Judge追踪")
    from ragas.llms.base import BaseRagasLLM
    from langchain_core.outputs import Generation, LLMResult
    from . import generation
    from .telemetry import current_metrics
    from .common import sha256_text
    from urllib.parse import urlsplit
    cfg = generation.llm_config()
    if not all(cfg.values()):
        raise ValueError("模型配置不完整")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("", encoding="utf-8")
    guard = threading.Lock()

    class ExistingModelJudge(BaseRagasLLM):
        calls = 0
        profile = {"host": urlsplit(cfg["base_url"]).hostname, "model": cfg["model"], "temperature": 0.1,
                   "json_output": True, "max_tokens": 4096, "max_calls": max_calls, "transport_retries": 0}

        def generate_text(self, prompt, n=1, **kwargs):
            if n != 1:
                raise ValueError("只允许单次完成，避免隐式多样本费用")
            with guard:
                if generation.llm_config() != cfg:
                    raise RuntimeError("judge_configuration_changed")
                if self.calls >= max_calls:
                    raise RuntimeError("judge_call_budget_exhausted")
                self.calls += 1
                content = prompt.to_string()
                usage = {"llm_calls": 0, "llm_seconds": 0.0}
                token = current_metrics.set(usage)
                record = {"call": self.calls, "host": urlsplit(cfg["base_url"]).hostname, "model": cfg["model"],
                          "temperature": 0.1, "max_tokens": 4096, "json_output": True,
                          "prompt": content, "prompt_sha256": sha256_text(content)}
                try:
                    # 仅在独立评测调用期间旁路记录真实请求/响应，不改动请求或产品实现。
                    from unittest.mock import patch
                    original = generation.requests.post
                    def observed(url, *args, **kwargs):
                        record["actual_request"] = kwargs.get("json")
                        response = original(url, *args, **kwargs)
                        record.update(http_status=response.status_code, raw_response=response.text)
                        return response
                    with patch.object(generation.requests, "post", observed):
                        text = generation.chat_completion([{"role": "user", "content": content}], max_tokens=4096, json_output=True)
                    record.update(status="received", response=text)
                    return LLMResult(generations=[[Generation(text=text)]])
                except Exception as error:
                    record.update(status="failed", error_type=type(error).__name__)
                    raise
                finally:
                    current_metrics.reset(token)
                    record["usage_and_timings"] = usage
                    # 原接口已屏蔽认证头；再对文本中的偶然密钥串脱敏。
                    with trace_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False).replace(cfg["api_key"], "[REDACTED]") + "\n")

        async def agenerate_text(self, prompt, n=1, **kwargs):
            return await asyncio.to_thread(self.generate_text, prompt, n=n, **kwargs)

        async def generate(self, prompt, n=1, **kwargs):
            # 不做传输重试；Ragas格式修复产生的额外调用也受同一总调用上限约束。
            return await self.agenerate_text(prompt, n=n, **kwargs)

        def is_finished(self, response):
            return True  # 复用接口已拒绝length和空响应。

    return ExistingModelJudge()
