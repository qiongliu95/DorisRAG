from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .common import ROOT, config, read_jsonl, sha256_file, write_json, write_jsonl


def normalize(text: str) -> str:
    return re.sub(r"\s+|`", "", text).lower()


def matches(row: dict, gold: dict) -> bool:
    evidence = normalize(row["chunk_text"] + "\n" + "\n".join(row.get("document_version_notices", [])) + "\n" + "\n".join(c["text"] for c in row.get("contexts", [])) + "\n" + "\n".join(row.get("version_note", [])))
    return (row["source_version"] == gold["version"] and row["relative_path"] == gold["path"]
            and all(normalize(term) in evidence for term in gold.get("terms", [])))


def evaluate(mode: str | None = None, answers=False, cases_path: Path | None = None) -> dict:
    from .pipeline import run_query
    from .retrieval import Retriever
    cfg = config()
    mode = mode or cfg["retrieval_mode"]
    top_k = cfg["top_k"]
    cases_path = cases_path or ROOT / "eval" / "cases.jsonl"
    cases = read_jsonl(cases_path)
    if not cases or not any(case.get("gold") for case in cases):
        raise ValueError("评测文件必须含至少一道有黄金证据的题目")
    rules_path = ROOT / "eval/scoring_rules_v2.json"
    rules = read_json(rules_path)
    groups = {case["id"]: evidence_groups(case, rules) for case in cases}
    retrieval = Retriever()
    results = []
    for case in cases:
        start = time.perf_counter()
        if case.get("top_k", top_k) != top_k:
            raise ValueError("本次题目需使用统一 top_k，避免汇总指标含义不一致")
        try:
            from .telemetry import request_trace
            with request_trace(False, {'entrypoint': 'eval', 'case_id': case['id']}, persist=False):
                execution = run_query(case["question"], case.get("version"), top_k, mode, not answers, retrieval)
        except Exception as error:
            execution = {"status": "service_or_pipeline_error", "error_type": type(error).__name__,
                         "evidence": [], "understanding": {"status": "failed"}}
        rows = execution["evidence"]
        seconds = time.perf_counter() - start
        targets = case.get("gold", [])
        ranks = [next((row["rank"] for row in rows if matches(row, gold)), None) for gold in targets]
        relevant = [row for row in rows if any(matches(row, gold) for gold in targets)]
        result = {**case, "target_ranks": ranks, "all_targets_hit": all(ranks) if targets else None,
                  "any_target_hit": any(ranks) if targets else None,
                  "reciprocal_rank": 1 / min(r for r in ranks if r) if any(ranks) else 0,
                  "gold_matched_chunks": len(relevant), "not_gold_matched_chunks": len(rows) - len(relevant),
                  "latency_seconds": seconds, "evidence": rows,
                  "stage_timings": execution.get("timings"),
                  "retrieval_candidates": execution.get("retrieval_candidates"),
                  "understanding": execution["understanding"], "execution_status": execution["status"],
                  "answer_correct": None, "groundedness": None, "hallucination": None,
                  "evidence_status": "normal" if targets and all(ranks) else "partial" if any(ranks) else "empty_but_chunks_exist",
                  "manual_review_required": True}
        if answers:
            result["generation"] = {key: value for key, value in execution.items() if key not in ("evidence", "understanding")}
        results.append(result)
    answerable = [r for r in results if r.get("gold")]
    report = {"build_id": retrieval.manifest["build_id"], "mode": mode, "cases": len(results),
              "scoring_version": "legacy_v1",
              "evidence_metric_scope": "legacy_returned_evidence_not_raw_recall",
              "retrieval_code_sha256": sha256_file(ROOT / "doris_rag" / "retrieval.py"),
              "dataset": cases_path.name,
              "top_k": top_k,
              "gold_cases": len(answerable), "cases_sha256": sha256_file(cases_path),
              f"any_target_hit_rate_at_{top_k}": sum(r["any_target_hit"] for r in answerable) / len(answerable),
              f"all_targets_hit_rate_at_{top_k}": sum(r["all_targets_hit"] for r in answerable) / len(answerable),
              f"mrr_at_{top_k}": sum(r["reciprocal_rank"] for r in answerable) / len(answerable),
              "not_gold_matched_rate": sum(r["not_gold_matched_chunks"] for r in answerable) / max(sum(len(r["evidence"]) for r in answerable), 1),
              "mean_latency_seconds": sum(r["latency_seconds"] for r in results) / len(results),
              "first_query_latency_seconds": results[0]["latency_seconds"],
              "mean_warm_latency_seconds": sum(r["latency_seconds"] for r in results[1:]) / max(len(results) - 1, 1),
              "answer_evaluation": "semantic_review_required" if answers else "not_run",
              "generation_statuses": dict(Counter(r.get("generation", {}).get("status", "not_requested") for r in results)),
              "generated_answers": sum(r.get("generation", {}).get("status") == "generated" for r in results),
              "limitations": ["非黄金匹配率是噪音代理值，未命中黄金规则不必然代表无关。",
                              "匹配规则验证具体片段和版本，不替代人工核对答案与证据。",
                              "小型诊断集不是独立大规模 benchmark。无答案题不纳入检索命中率分母。"]}
    directory = ROOT / "reports" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + cases_path.stem + "_" + mode)
    directory.mkdir(parents=True, exist_ok=False)
    write_jsonl(directory / "cases.jsonl", results)
    write_json(directory / "summary.json", report)
    # 生成后返回的 evidence 可能仅有已引用片段；保留旧分作对照，不再称为原始召回。
    revised = [{"id": r["id"], "answer_semantics": semantic_from_labels({}),
                "retrieval": metric(None, "not_evaluated", "缺少原始候选和实际送模追踪") if answers
                else score_groups(r["evidence"], groups[r["id"]], enriched=True)} for r in results]
    write_json(directory / "scoring_v2.json", {"scoring_version": rules["version"], "rules_sha256": sha256_file(rules_path),
               "scope": "final_selected_enriched_not_raw_candidates", "cases": revised})
    report["report_directory"] = str(directory)
    return report


def metric(value=None, status="not_evaluated", reason="", **details):
    """缺失、失败和不适用不混成0；只有明确观察到失败时才给0。"""
    return {"status": status, "value": value, "reason": reason, **details}


def evidence_groups(case, rules=None):
    groups = [{"id": f"g{n}", "alternatives": [[g]]} for n, g in enumerate(case.get("gold", []), 1)]
    change = (rules or {}).get("cases", {}).get(case["id"])
    if change:
        from .common import sha256_text
        if sha256_text(case.get("question", "")) != change["question_sha256"]:
            raise ValueError("规则与题目正文身份不一致：" + case["id"])
        if "groups" in change:
            return change["groups"]
        for group, alternatives in zip(groups, change.get("extra_alternatives", [])):
            group["alternatives"].extend(alternatives)
    return groups


def task_rubric(case_id, rules, default=None):
    return rules.get("semantic_rubrics", {}).get(case_id, default)


def matches_v2(row, gold, enriched):
    if row["source_version"] != gold["version"] or row["relative_path"] != gold["path"]:
        return False
    text = row["chunk_text"]
    if enriched:
        text += "\n" + "\n".join(row.get("document_version_notices", []) + row.get("version_note", []))
        text += "\n" + "\n".join(c["text"] for c in row.get("contexts", []))
    text = text.replace("`", "").lower()
    for term in gold.get("terms", []):
        term = term.replace("`", "").lower().strip()
        pattern = r"\s*".join(re.escape(p) for p in re.split(r"\s+", term))
        # 标识符/数字/版本必须完整，不能让5命中50、V1命中V12；保留原文词界。
        if re.match(r"[a-z0-9_.]", term):
            pattern = r"(?<![a-z0-9_.])" + pattern
        if re.search(r"[a-z0-9_.]$", term):
            pattern += r"(?![a-z0-9_.])"
        if not term or not re.search(pattern, text):
            return False
    return True


def score_groups(rows, groups, enriched=False):
    if not groups:
        return metric(status="not_applicable", reason="无必要证据金标准，不纳入召回分母")
    if rows is None:
        return metric(reason="此阶段没有可证明完整的有序记录")
    if any(row.get("context_status") in ("source_changed", "source_error", "untrusted_source") for row in rows):
        return metric(status="invalid_data", reason="证据来源完整性未通过，不能据陈旧片段给可靠分数")
    if any(not g.get("alternatives") or any(not a or any(not t.get("terms") for t in a) for a in g["alternatives"]) for g in groups):
        return metric(status="invalid_data", reason="事实证据组缺少非空替代项或必要词句；页面覆盖应使用单独指标")
    ranks, alternatives = [], []
    for group in groups:
        options = []
        for alternative in group["alternatives"]:
            hits = []
            for target in alternative:
                hits.append(next((i for i, row in enumerate(rows, 1) if matches_v2(row, target, enriched)), None))
            options.append(max(hits) if hits and all(hits) else None)
        ranks.append(min(x for x in options if x is not None) if any(x is not None for x in options) else None)
        alternatives.append(options)
    hit = sum(r is not None for r in ranks)
    return metric(hit / len(groups), "evaluated", "必要知识组覆盖；同组任一等价来源可满足，跨版本不互换",
                  target_ranks=ranks, alternative_ranks=alternatives, any_hit=hit > 0,
                  all_hit=hit == len(groups), hit_groups=hit, total_groups=len(groups),
                  reciprocal_rank=1 / min(r for r in ranks if r is not None) if hit else 0)


def fraction_labels(items, field, label):
    if not items:
        return metric(reason="缺少" + label + "逐项审核标签")
    ids = [x.get("id") for x in items]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        return metric(status="invalid_data", reason="审核项ID必须存在且唯一")
    known = [x[field] for x in items if type(x.get(field)) is bool]
    if len(known) != len(items):
        return metric(status="partially_evaluated", reason="存在未知标签，不以已审子集均值冒充全量分数",
                      reviewed=len(known), total=len(items))
    return metric(sum(known) / len(items), "evaluated", label, numerator=sum(known), denominator=len(items))


def semantic_from_labels(labels, *, decomposition_exhaustiveness_human_confirmed=False, reference_reviewed=False):
    claims = labels.get("answer_claims", [])
    result = {"factual_precision": fraction_labels(claims, "correct", "答案断言正确率"),
              "faithfulness": fraction_labels(claims, "supported", "答案断言证据支撑率"),
              "answer_completeness": fraction_labels(labels.get("reference_points", []), "covered", "参考必要要点覆盖率"),
              "answer_relevancy": fraction_labels(labels.get("relevance_items", []), "satisfied", "问题要求相关性"),
              "boundary_handling": fraction_labels(labels.get("boundary_items", []), "passed", "边界行为"),
              "version_applicability": fraction_labels(labels.get("version_items", []), "passed", "正文版本和条件适用性"),
              "citation_entailment": fraction_labels(labels.get("citation_claims", []), "supported", "引用与断言蕴含")}
    # 列表内标签齐全不代表整题分母已审定；保留局部计数，不发布整题比例。
    for name, items, field, confirmed, flag in (
        ("factual_precision", claims, "correct", decomposition_exhaustiveness_human_confirmed,
         "decomposition_exhaustiveness_human_confirmed"),
        ("faithfulness", claims, "supported", decomposition_exhaustiveness_human_confirmed,
         "decomposition_exhaustiveness_human_confirmed"),
        ("answer_completeness", labels.get("reference_points", []), "covered", reference_reviewed,
         "reference_reviewed"),
    ):
        if items and confirmed is not True and result[name]["status"] != "invalid_data":
            known = [item[field] for item in items if type(item.get(field)) is bool]
            result[name] = metric(reason="分母范围未审核，不将已列项目比例作为整题指标",
                                  scope_status="unreviewed", required_scope_confirmation=flag,
                                  listed_items=len(items), reviewed=len(known), unknown=len(items) - len(known),
                                  confirmed_true=sum(known), confirmed_false=len(known) - sum(known),
                                  denominator=None)
    requirements = labels.get("task_requirements", [])
    task = fraction_labels(requirements, "passed", "全部必要任务要求")
    if task["status"] == "evaluated":
        task["value"] = int(all(x["passed"] for x in requirements))
    elif task["status"] == "partially_evaluated" and any(x.get("passed") is False for x in requirements):
        task.update(value=0, status="evaluated_failure_with_missing_labels",
                    reason="至少一项必要要求明确失败；其余未评估项不消失")
    result["task_completion"] = task
    p, r = result["factual_precision"], result["answer_completeness"]
    result["factual_correctness_f1"] = metric(reason="需要完整的答案正确标签和参考要点覆盖标签")
    if p["status"] == r["status"] == "evaluated":
        value = 2 * p["value"] * r["value"] / (p["value"] + r["value"]) if p["value"] + r["value"] else 0.0
        result["factual_correctness_f1"] = metric(value, "evaluated", "提供的完整审核标签之标准P/R调和均值", method="provided_review_labels")
    elif any(m.get("scope_status") == "unreviewed" for m in (p, r)):
        result["factual_correctness_f1"] = metric(reason="正确性或完整性的分母范围未审核",
                                                 scope_status="unreviewed", denominator=None)
    return result


def review_subject(sample, rules_sha256):
    from .common import sha256_text
    return sha256_text(json.dumps({"sample": sample, "rules_sha256": rules_sha256}, ensure_ascii=False, sort_keys=True))


def score_reviewed_labels(sample, annotation, rules_sha256):
    """发布标注分前绑定完整样本和规则，防止把另一题/旧答案的审核串用。"""
    if (not rules_sha256 or not isinstance(annotation.get("reviewer"), str) or not annotation["reviewer"].strip()
            or annotation.get("subject_sha256") != review_subject(sample, rules_sha256)):
        return {"metrics": {name: metric(status="invalid_data", reason="审核人、样本或规则绑定不一致")
                            for name in semantic_from_labels({})}}
    return {"metrics": semantic_from_labels(annotation.get("labels", {}),
                decomposition_exhaustiveness_human_confirmed=annotation.get("decomposition_exhaustiveness_human_confirmed"),
                reference_reviewed=annotation.get("reference_reviewed")),
            "reviewer": annotation["reviewer"], "subject_sha256": annotation["subject_sha256"],
            "decomposition_exhaustiveness_human_confirmed": annotation.get("decomposition_exhaustiveness_human_confirmed") is True,
            "reference_reviewed": annotation.get("reference_reviewed") is True}


def context_precision(rows, labels):
    if rows is None or not rows:
        return metric(reason="没有明确顺序的上下文")
    keys = [r.get("chunk_id") for r in rows]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        return metric(status="invalid_data", reason="上下文ID缺失或重复")
    if any(type(labels.get(k)) is not bool for k in keys):
        return metric(reason="缺少该阶段每条上下文的相关/无关标签")
    relevance = [labels[k] for k in keys]
    count, numerator = 0, 0.0
    for rank, relevant in enumerate(relevance, 1):
        count += relevant
        numerator += (count / rank) if relevant else 0
    return metric(numerator / count if count else 0.0, "evaluated", "按完整相关性标签计算排序AP（Ragas Context Precision口径）",
                  precision=count / len(rows), noise_rate=1 - count / len(rows), relevant=count, contexts=len(rows))


def sent_contexts(trace):
    """只读实际生成请求的证据块；不把最终引用列表倒推成送模上下文。"""
    calls = [c for c in trace.get("model_calls", []) if not c.get("request", {}).get("response_format")]
    if len(calls) != 1:
        return None
    texts = [m["content"] for m in calls[0]["request"].get("messages", []) if m.get("role") == "user" and isinstance(m.get("content"), str)]
    text = "\n".join(texts)
    boundaries = list(re.finditer(r"\n检索来源范围：[^\n]+\n\n(?=\[证据 \d+\]\n标题：)", text))
    if not boundaries:
        return None
    text = text[boundaries[-1].end():]
    rows = []
    # 要求标题/来源/链接头，避免用户材料或代码里的“[证据 1]”伪造上下文。
    pattern = r"(?m)^\[证据 (\d+)\]\n标题：([^\n]+)\n来源目录版本：([^（\n]+)[^\n]*\n章节：([^\n]*)\n链接：(https://doris\.apache\.org/zh-CN/docs/[^\n]+)\n"
    found = list(re.finditer(pattern, text))
    if len(found) != len(re.findall(r"(?m)^\[证据 \d+\]$", text)):
        return None
    for i, match in enumerate(found):
        version, path = re.match(r"https://doris\.apache\.org/zh-CN/docs/([^/]+)/(.+)", match[5]).groups()
        if version != match[3]:
            return None
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        rows.append({"rank": int(match[1]), "source_version": version, "relative_path": path,
                     "chunk_id": "sent:" + match[1], "chunk_text": text[match.start():end],
                     "source_url": match[5], "title": match[2]})
    return rows or None


def ranking_rows(ranking):
    values = ranking.get("rows", ranking.get("ranking"))
    if values is None:
        return None
    return [{**r, "source_version": r.get("source_version", ranking.get("version")),
             "relative_path": r.get("relative_path", r.get("path", "")),
             "chunk_text": r.get("chunk_text", r.get("text", ""))} for r in values]


def source_match(row, target):
    # URL通常没有扩展名；这里只是必要页面覆盖，不能代替事实级匹配。
    return row["source_version"] == target["version"] and re.sub(r"\.mdx?$", "", row["relative_path"]) == re.sub(r"\.mdx?$", "", target["path"])


def source_coverage(rows, targets):
    if rows is None:
        return metric(reason="缺少此阶段追踪")
    if not targets:
        return metric(status="not_applicable", reason="无必要来源判据")
    ranks = [next((i for i, row in enumerate(rows, 1) if source_match(row, t)), None) for t in targets]
    return metric(sum(x is not None for x in ranks) / len(ranks), "evaluated", "页面级来源覆盖，不等于事实正确或Recall@K",
                  target_ranks=ranks, all_sources_present=all(x is not None for x in ranks))


def evidence_identity(rows, source_root):
    if not rows:
        return metric(reason="无可核验的原文来源")
    verified = []
    for row in rows:
        if not all(row.get(k) for k in ("original_path", "source_sha256", "source_start_line", "source_end_line")):
            return metric(reason="来源哈希或原文行号缺失")
        path = Path(row["original_path"]).resolve()
        if not path.is_relative_to(source_root.resolve()):
            return metric(status="invalid_data", reason="原文路径不在冻结配置的语料根目录内")
        if not path.is_file():
            return metric(reason="原文文件当前不可访问")
        n = len(path.read_text(encoding="utf-8-sig").splitlines())
        verified.append(sha256_file(path) == row["source_sha256"] and 1 <= row["source_start_line"] <= row["source_end_line"] <= n)
    return metric(int(all(verified)), "evaluated", "当前原文SHA和定位行有效，不代表引用语义成立", checked=len(verified))


def aggregate_metrics(values):
    scored = [v for v in values if v.get("value") is not None and v["status"].startswith("evaluated")]
    applicable = [v for v in values if v["status"] != "not_applicable"]
    mean = sum(v["value"] for v in scored) / len(scored) if scored else None
    complete = bool(applicable) and len(scored) == len(applicable)
    return metric(mean if complete else None, "evaluated" if complete else "partially_evaluated" if scored else "not_evaluated",
                  "缺少适用题评分时总体值为空；已评子集均值单列，不能代表全体",
                  evaluated_subset_mean=mean, evaluated=len(scored), total=len(values),
                  applicable=len(applicable), not_applicable=len(values) - len(applicable), missing=len(applicable) - len(scored))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def replay(baseline: Path, output: Path, rules_path: Path | None = None):
    """仅重评分冻结产物；不加载Retriever、不生成问题、不调用模型。"""
    import hashlib
    rules_path = rules_path or ROOT / "eval/scoring_rules_v2.json"
    rules = read_json(rules_path)
    inventory = read_json(baseline / "historical_inventory.json")
    inputs = {"rules": sha256_file(rules_path), "dataset": sha256_file(baseline / "selected_cases.jsonl")}
    for name in ("offline_cases.jsonl", "historical_evaluation.json", "historical_inventory.json", "manifest.json",
                 "snapshot/eval/cases.jsonl", "snapshot/eval/v1_final_acceptance_20261024.json"):
        inputs[name] = sha256_file(baseline / name)
    original = {c["id"]: c for c in read_jsonl(baseline / "snapshot/eval/cases.jsonl")}
    selected = {c["id"]: original.get(c["id"], c) for c in read_jsonl(baseline / "selected_cases.jsonl")}
    if sha256_file(baseline / "snapshot/eval/cases.jsonl") != rules["base_cases_sha256"]:
        raise ValueError("规则不属于该冻结原始题集")
    if sha256_file(baseline / "snapshot/eval/v1_final_acceptance_20261024.json") != rules["base_cards_sha256"]:
        raise ValueError("规则不属于该冻结验收卡版本")
    clarifications = baseline / "snapshot/eval/V1_ACCEPTANCE_CLARIFICATIONS_20261024.md"
    if sha256_file(clarifications) != rules["clarifications_sha256"]:
        raise ValueError("冲突补充判据身份不一致")
    inputs["clarifications"] = rules["clarifications_sha256"]
    # 等价来源及冲突依据均固定原文哈希；失效即停，不用漂移资料静默改分。
    for source in rules["verified_sources"]:
        if sha256_file(ROOT / source["file"]) != source["sha256"]:
            raise ValueError("评测依据原文已变化：" + source["file"])
        inputs[source["file"]] = source["sha256"]
    for name, expected in inventory["files"].items():
        actual = sha256_file(baseline / "historical_inputs" / name)
        if actual != expected:
            raise ValueError("冻结追踪哈希不一致：" + name)
        inputs[name] = actual
    prior = read_json(baseline / "historical_evaluation.json")
    expected_prior = read_json(baseline / "final_checks.json")["historical_evaluation_sha256"]
    if sha256_file(baseline / "historical_evaluation.json") != expected_prior:
        raise ValueError("历史语义复核结果身份不一致")
    prior_reviews = {r["id"]: r["semantic_review"] for r in prior["records"]}
    source_root = ROOT / read_json(baseline / "snapshot/config.json")["source_root"]
    cards = {c["id"]: c for c in read_json(baseline / "snapshot/eval/v1_final_acceptance_20261024.json")["cases"]}
    offline = []
    for record in read_jsonl(baseline / "offline_cases.jsonl"):
        case = selected[record["id"]]
        result = record.get("result")
        groups = evidence_groups(case, rules)
        rows = result["evidence"] if result else None
        ready = result and result["understanding"]["status"] == "ready"
        eligible = rows if ready else None
        saved = {k: record[k] for k in ("raw_target_ranks", "enriched_target_ranks", "document_target_ranks") if k in record}
        delivery = score_groups(rows, groups, True)
        offline.append({"id": case["id"], "status": record["status"], "legacy_saved_v1": saved,
            "raw_final_selected": score_groups(eligible, groups), "enriched_final_selected": score_groups(eligible, groups, True),
            "pipeline_evidence_delivery": delivery, "raw_candidate_recall": metric(reason="此冻结免费试算未记录原始候选池"),
            "actual_model_input_coverage": metric(reason="未生成答案，没有实际送模输入"),
            "semantic": semantic_from_labels({}),
            "task_rubric": task_rubric(case["id"], rules),
            "attribution": "not_executed" if not result else "understanding_blocked" if not ready and groups else
                "scoring_rule_false_negative_corrected" if saved.get("enriched_target_ranks") and not all(saved["enriched_target_ranks"]) and delivery.get("all_hit") else
                "no_gold_for_boundary_semantics" if not groups else "gold_rule_hit_not_semantic_pass"})
    historical, cohorts = [], []
    for cases_file in sorted((baseline / "historical_inputs/reports").rglob("cases.jsonl")):
        start_index = len(historical)
        directory = cases_file.parent
        manifest_file = directory / ("manifest.json" if (directory / "manifest.json").exists() else "run_manifest.json")
        manifest = read_json(manifest_file)
        identity_files = {k: v for k, v in manifest["files"].items() if k.startswith(("doris_rag/", "ui/")) or k in ("config.json", "requirements-lock.txt")}
        identity = hashlib.sha256(json.dumps(identity_files, sort_keys=True).encode()).hexdigest()
        for record in read_jsonl(cases_file):
            result = record["result"]
            td = "traces" if (directory / "traces").exists() else "turn_traces"
            trace = read_json(directory / td / (record["message_id"] + ".json"))
            turn = trace.get("turn", trace.get("query_pipeline_input", {}))
            effective_query = turn.get("query") or record["question"]
            sent = sent_contexts(trace)
            rankings = trace.get("rankings", trace.get("retrieval_rankings", []))
            card_id = next((c for c in cards if re.match(re.escape(c) + r"(?:_|$)", record["case"])), None)
            targets = [{"version": b["source_version"], "path": b["file"].split("version-" + b["source_version"] + "/", 1)[1]}
                       for b in cards[card_id]["knowledge_basis"] if b.get("source_version")] if card_id else []
            candidates = [{"query": r.get("query"), "version": r.get("version"),
                           "source_coverage": source_coverage(ranking_rows(r), targets)} for r in rankings]
            key = directory.name + "/" + record["case"]
            review = prior_reviews.get(key, {})
            semantic = semantic_from_labels({})
            task = review.get("task")
            if task in ("pass", "fail"):
                semantic["task_completion"] = metric(int(task == "pass"), "evaluated", "复用冻结历史复核；未确认审核人身份，不作为新增独立人工审核", method="historical_review")
            boundary = review.get("boundary", "unknown")
            if boundary.startswith("pass_"):
                semantic["boundary_handling"] = metric(1, "evaluated", "复用冻结人工边界复核：" + boundary)
            elif boundary == "not_applicable":
                semantic["boundary_handling"] = metric(status="not_applicable", reason="此记录无既定边界判据")
            citations = set(map(int, re.findall(r"\[证据\s*(\d+)\]", result.get("answer", ""))))
            numbers = {r["rank"] for r in sent} if sent is not None else None
            coverage = source_coverage(sent, targets)
            candidate_seen = targets and all(any(source_match(row, t) for r in rankings for row in (ranking_rows(r) or [])) for t in targets)
            reasons = []
            if coverage.get("all_sources_present") is False:
                reasons.append("required_source_not_selected" if candidate_seen else "required_source_not_in_recorded_candidates")
                # 页面在候选不必然代表必要事实在候选；仅复用已核对具体片段的冻结归因。
                if any(a.startswith("ranking_or_final_selection_") for a in review.get("attribution", [])):
                    reasons.append("ranking_or_selection")
            if task == "fail":
                reasons.append("task_incomplete_by_frozen_review")
            calls = trace.get("model_calls", [])
            statuses = [c.get("http_status") for c in calls]
            http_success = metric(sum(s == 200 for s in statuses) / len(statuses), "evaluated", "仅已记录模型HTTP请求，不等于任务成功率") if statuses and all(type(s) is int for s in statuses) else metric(reason="请求状态记录不全")
            historical.append({"id": key, "code_identity": identity, "build_id": result.get("build_id"),
                "origin": "frozen_history", "trace_path": str((directory / td / (record["message_id"] + ".json")).resolve()),
                "review_provenance": {"source": str((baseline / "historical_evaluation.json").resolve()),
                                      "method": prior.get("semantic_review_method"), "human_reviewer_confirmed": False},
                "session_id": record.get("session_id"), "message_id": record["message_id"],
                "turn_kind": result.get("turn_kind"), "effective_query": effective_query,
                "question": record["question"], "status": result["status"], "card": card_id,
                "raw_candidate_lists": candidates, "sent_source_coverage": coverage,
                "sent_context_count": len(sent) if sent is not None else None,
                "citation_number_validity": metric(int(bool(citations) and citations.issubset(numbers)), "evaluated", "仅编号存在，不验证断言支撑") if numbers is not None else metric(reason="缺少实际送模内容"),
                "source_identity": evidence_identity(result.get("evidence", []), source_root),
                "evidence_within_plan_scope": metric(int(all(e["source_version"] in result["understanding"]["versions"] for e in result.get("evidence", []))), "evaluated", "仅出处在计划范围内，不推导正文适用性")
                    if result.get("evidence") and result.get("understanding", {}).get("versions") else metric(reason="无证据或计划范围记录"),
                "context_precision": context_precision(sent, {}), "semantic": semantic, "frozen_review": review,
                "acceptance_verdict": "fail" if task == "fail" else "not_determined",
                "logged_model_http_success": http_success,
                "logged_model_calls": len(calls), "call_count_consistent": len(calls) == result.get("timings", {}).get("llm_calls"),
                "attribution": reasons, "timings": result.get("timings", {}),
                "ragas_input": {"user_input": effective_query, "original_user_input": record["question"],
                                "response": result.get("answer", ""),
                                "retrieved_contexts": [r["chunk_text"] for r in sent] if sent is not None else None,
                                "contexts_complete": sent is not None,
                                "source_versions": result["understanding"].get("versions"),
                                "reference": None, "reference_reviewed": False,
                                "reference_applicability_reviewed": False, "expected_behavior": None,
                                "known_critical_failures": reasons if task == "fail" else [],
                                "task_rubric": task_rubric(card_id, rules, cards[card_id]["pass_conditions"]) if card_id else None}})
        cohort = historical[start_index:]
        times = [r["timings"].get("total_seconds") for r in cohort]
        measured = [t for t in times if type(t) in (int, float) and t >= 0]
        cohorts.append({"name": directory.name, "code_identity": identity, "n": len(cohort),
            "task_completion": aggregate_metrics([r["semantic"]["task_completion"] for r in cohort]),
            "mean_total_seconds": sum(measured) / len(measured) if measured else None,
            "latency_measured": len(measured), "latency_total": len(times),
            "resource_peak": metric(reason="无该批次资源峰值采样"),
            "factual_correctness": metric(reason="无审核后的断言/参考答案，且本轮未授权Judge")})
    if output.exists():
        raise FileExistsError("不覆盖已有评分目录：" + str(output))
    output.mkdir(parents=True)
    # 结果正文不加当前时间，重放得到相同内容哈希；调用时间不混进指标身份。
    write_json(output / "manifest.json", {"scoring_version": rules["version"], "inputs": inputs,
        "evaluator_sha256": sha256_file(Path(__file__)), "new_model_calls": 0,
        "semantic_adapter_sha256": sha256_file(Path(__file__).with_name("evaluation_semantics.py")),
        "scope": "retrospective_rule_validation_not_new_quality_acceptance"})
    write_jsonl(output / "offline_cases.jsonl", offline)
    write_jsonl(output / "historical_cases.jsonl", historical)
    scored_delivery = [r["pipeline_evidence_delivery"] for r in offline if r["pipeline_evidence_delivery"]["status"] == "evaluated"]
    group_hits = sum(m["hit_groups"] for m in scored_delivery)
    group_total = sum(m["total_groups"] for m in scored_delivery)
    summary = {"version": rules["version"], "offline_cases": len(offline), "historical_cases": len(historical),
        "pipeline_gold_coverage": aggregate_metrics([r["pipeline_evidence_delivery"] for r in offline]),
        "pipeline_all_groups_hit": aggregate_metrics([metric(int(r["pipeline_evidence_delivery"]["all_hit"]), "evaluated")
            if r["pipeline_evidence_delivery"]["status"] == "evaluated" else r["pipeline_evidence_delivery"] for r in offline]),
        "observed_evidence_groups": {"hit": group_hits, "scored": group_total,
            "micro_on_scored": group_hits / group_total if group_total else None,
            "reason": "仅已执行题的必要组微平均，不作为全题集总分"},
        "stage_aggregates": {stage: aggregate_metrics([r[stage] for r in offline])
                              for stage in ("raw_final_selected", "enriched_final_selected")},
        "cohorts": cohorts, "corrected_rule_false_negatives": [r["id"] for r in offline if r["attribution"] == "scoring_rule_false_negative_corrected"],
        "new_paid_calls": 0, "limits": ["v2为已见样本的回溯规则修订，不是产品改进或独立留出成绩。", "不合并不同代码批次，不将缺失评分记为0或通过。"]}
    write_json(output / "summary.json", summary)
    return summary


def compare_scores(left: Path, right: Path):
    """只比较同题集、同规则、同计分代码的结果；规则修订不当成产品进步。"""
    a, b = read_json(left / "manifest.json"), read_json(right / "manifest.json")
    same = (a["scoring_version"] == b["scoring_version"] and a["evaluator_sha256"] == b["evaluator_sha256"]
            and all(a["inputs"][key] == b["inputs"][key] for key in ("rules", "dataset")))
    if not same:
        return {"comparable": False, "reason": "题集、规则或计分代码不同；只能作口径变更对照，不能算质量提升"}
    x, y = read_json(left / "summary.json"), read_json(right / "summary.json")
    deltas = {}
    for key in ("pipeline_gold_coverage", "pipeline_all_groups_hit"):
        u, v = x[key], y[key]
        if u["value"] is None or v["value"] is None or u["evaluated"] != v["evaluated"]:
            deltas[key] = metric(reason="全体评分不完整或有效分母不同，不计算总体提升")
        else:
            deltas[key] = metric(v["value"] - u["value"], "evaluated")
    return {"comparable": True, "deltas": deltas, "cohorts_must_be_compared_separately": True}


class _CurrentCapture:
    """独立Eval进程的旁路记录；不改变生产函数参数、返回值和排序。"""
    def __init__(self, cfg, output, max_calls):
        self.cfg, self.output, self.max_calls = cfg, output, max_calls
        self.trace = None
        self.network_calls = 0
        self.consecutive_errors = 0
        self.blocked = None

    def save(self):
        if self.trace is not None:
            secret = self.cfg.get("api_key")
            text = json.dumps(self.trace, ensure_ascii=False, indent=2)
            path = self.output / "traces" / (self.trace["case_id"] + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text.replace(secret, "[REDACTED]") if secret else text, encoding="utf-8")
            temporary.replace(path)

    def post(self, original, url, *args, **kwargs):
        import copy
        if not str(url).endswith("/chat/completions"):
            return original(url, *args, **kwargs)
        if url != self.cfg["base_url"].rstrip("/") + "/chat/completions":
            raise RuntimeError("model_endpoint_changed")
        call = {"request": copy.deepcopy(kwargs.get("json")), "network_sent": False, "status": "started"}
        self.trace["model_calls"].append(call)
        started = time.perf_counter()
        try:
            if self.blocked or self.network_calls >= self.max_calls:
                call["status"] = "blocked_without_network"
                raise RuntimeError(self.blocked or "model_call_budget_exhausted")
            self.network_calls += 1
            call["network_sent"] = True
            self.save()
            response = original(url, *args, **kwargs)
            call.update(http_status=response.status_code, raw_response=response.text, status="received")
            self.consecutive_errors = self.consecutive_errors + 1 if response.status_code >= 400 else 0
            if response.status_code in (401, 403, 429):
                self.blocked = "provider_http_" + str(response.status_code)
            return response
        except Exception as error:
            if call["network_sent"]:
                self.consecutive_errors += 1
            call.update(error_type=type(error).__name__)
            raise
        finally:
            if self.consecutive_errors >= 3:
                self.blocked = "three_consecutive_provider_errors"
            call["seconds"] = time.perf_counter() - started
            self.save()


def _current_score(case, result, trace, rules, source_root):
    groups = evidence_groups(case, rules)
    ready = result.get("understanding", {}).get("status") == "ready"
    raw, enriched = trace.get("selected_raw_rows"), trace.get("enriched_rows")
    sent = sent_contexts(trace)
    # 实际提示URL不含扩展名；只按同一编号且同一来源映射到记录的原文件名。
    mapped = [] if sent is not None else None
    for row in sent or []:
        known = next((r for r in trace.get("generation_rows", []) if r["rank"] == row["rank"] and source_match(row, {"version": r["source_version"], "path": r["relative_path"]})), None)
        if known is None:
            mapped = None
            break
        mapped.append({**row, "relative_path": known["relative_path"]})
    citations = set(map(int, re.findall(r"\[证据\s*(\d+)\]", result.get("answer", ""))))
    numbers = {r["rank"] for r in sent} if sent is not None else None
    stages = {"retrieved_at_k": score_groups(trace.get("retrieved_rows", [])[:trace["top_k"]], groups) if "retrieved_rows" in trace else score_groups(None, groups),
              "raw_final_selected": score_groups(raw, groups), "enriched_final_selected": score_groups(enriched, groups, True),
              "actual_model_input_coverage": score_groups(mapped, groups),
              "pipeline_evidence_delivery": score_groups([] if not ready and result.get("understanding") else enriched, groups, True)}
    rows = result.get("evidence", [])
    return {**stages, "candidate_lists": [{"query": r["query"], "version": r["version"], "observed_count": len(r["rows"]),
                   "at_requested_k": score_groups(r["rows"][:trace["top_k"]], groups)} for r in trace.get("rankings", [])],
            "citation_number_validity": metric(int(bool(citations) and citations.issubset(numbers)), "evaluated", "结构检查，不代表引用支撑") if numbers is not None else metric(reason="无实际生成请求"),
            "source_identity": evidence_identity(rows, source_root),
            "context_precision": context_precision(sent, {}), "semantic": semantic_from_labels({}),
            "historical_review_applied": False, "quality_status": "pending_review",
            "attribution": ["understanding_blocked"] if groups and not ready and result.get("understanding") else
                           ["necessary_evidence_missing"] if stages["pipeline_evidence_delivery"].get("all_hit") is False else []}


def _save_current_report(output, records, capture, cases, manifest):
    from collections import Counter
    names = ("retrieved_at_k", "raw_final_selected", "enriched_final_selected", "actual_model_input_coverage",
             "pipeline_evidence_delivery", "citation_number_validity", "source_identity", "context_precision")
    totals = {name: aggregate_metrics([r["scores"][name] for r in records]) for name in names}
    for name in semantic_from_labels({}):
        totals[name] = aggregate_metrics([r["scores"]["semantic"][name] for r in records])
    scored = [r["scores"]["pipeline_evidence_delivery"] for r in records if r["scores"]["pipeline_evidence_delivery"]["status"] == "evaluated"]
    all_hit = [metric(int(m["all_hit"]), "evaluated") if m["status"] == "evaluated" else m for m in [r["scores"]["pipeline_evidence_delivery"] for r in records]]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "usage_recorded_calls": 0}
    for record in records:
        if record["origin"] != "current_live_run":
            continue
        for call in read_json(output / record["trace_path"]).get("model_calls", []):
            try:
                value = json.loads(call.get("raw_response", "{}")).get("usage", {})
            except (ValueError, TypeError):
                value = {}
            if all(type(value.get(k)) is int for k in ("prompt_tokens", "completion_tokens")):
                usage["usage_recorded_calls"] += 1
                for key in ("prompt_tokens", "completion_tokens"):
                    usage[key] += value[key]
    measured = [r["result"]["timings"]["total_seconds"] for r in records if type(r["result"].get("timings", {}).get("total_seconds")) in (int, float) and r["result"]["timings"]["total_seconds"] >= 0]
    summary = {"run_id": manifest["run_id"], "origin": manifest.get("scoring_origin", "current_live_run"), "planned": len(cases), "recorded": len(records),
        "completed": len(records) == len(cases), "pipeline_attempted": sum(r["execution_status"] in ("completed", "failed") for r in records),
        "reused_compatible_cases": sum(r["execution_status"] == "reused" for r in records),
        "statuses": dict(Counter(r["result"].get("status") for r in records)), "metrics": totals,
        "all_required_groups_hit": aggregate_metrics(all_hit),
        "observed_gold_groups": {"hit": sum(m["hit_groups"] for m in scored), "total": sum(m["total_groups"] for m in scored)},
        "network_model_calls": capture.network_calls, "max_model_calls": capture.max_calls, "judge_calls": 0,
        "provider_block": capture.blocked, "provider_usage": usage, "quality_verdict": "not_determined",
        "mean_pipeline_seconds": sum(measured) / len(measured) if measured else None, "latency_measured": len(measured),
        "latency_origin": "original_generation_run",
        "limitations": ["缺少逐断言/上下文/任务审核，不把生成或检索命中当任务完成。", "历史复核只属于原运行，不应用于新答案。", "候选按子查询/版本单独评，不虚构全局候选排名。"]}
    write_json(output / "summary.json", summary)
    write_jsonl(output / "cases.jsonl", records)
    reviews, text = [], ["# 当前35题实际问答与评测", "", "语义项目待真实审核；本页不是人工通过结论。", ""]
    for record in records:
        trace = read_json(output / record["trace_path"])
        case = next(c for c in cases if c["id"] == record["id"])
        sample = {"user_input": case["question"], "response": record["result"].get("answer", ""),
                  "retrieved_contexts": [r["chunk_text"] for r in sent_contexts(trace) or []],
                  "source_versions": record["result"].get("understanding", {}).get("versions"),
                  "task_rubric": task_rubric(case["id"], read_json(output / "rules.json"))}
        reviews.append({"id": case["id"], "run_id": manifest["run_id"], "trace_path": str((output / record["trace_path"]).resolve()),
            "sample": sample, "subject_sha256": review_subject(sample, manifest["rules_sha256"]), "reviewer": None,
            "labels": {}, "status": "pending_human_review", "context_ids": [r["chunk_id"] for r in sent_contexts(trace) or []],
            "existing_guidance": case.get("expected_keypoints"),
            "candidate_reference": case.get("reference"), "reference_reviewed": False})
        text += ["## " + case["id"] + "：" + case["question"], "", "执行状态：`" + record["result"].get("status", "unknown") + "`；质量：待审核。", "",
                 record["result"].get("answer", "未取得答案"), "", "[完整Trace](" + (output / record["trace_path"]).as_posix() + ")", "", "实际引用依据：", ""]
        for e in record["result"].get("evidence", []):
            text.append(f"- [{e['rank']}] {e['source_version']} · [{e['relative_path']}]({e['original_path'].replace(chr(92), '/')}:" + str(e['source_start_line']) + f")，第{e['source_start_line']}–{e['source_end_line']}行。")
        text.append("")
    write_jsonl(output / "review_queue.jsonl", reviews)
    (output / "answers.md").write_text("\n".join(text), encoding="utf-8")
    return summary


def rescore_current(source: Path, output: Path, annotations_path: Path | None = None):
    """只读已有实际运行；可导入真实审核标注，不触发检索或模型。"""
    import copy
    import shutil
    if output.exists():
        raise FileExistsError("不覆盖评分结果")
    manifest = copy.deepcopy(read_json(source / "manifest.json"))
    cases = read_jsonl(source / "dataset.jsonl")
    rules = read_json(source / "rules.json")
    if sha256_file(source / "dataset.jsonl") != manifest["dataset_sha256"] or sha256_file(source / "rules.json") != manifest["rules_sha256"]:
        raise ValueError("运行题集或规则哈希不一致")
    if annotations_path is None and (source / "provided_annotations.jsonl").exists():
        annotations_path = source / "provided_annotations.jsonl"
    annotations = read_jsonl(annotations_path) if annotations_path else []
    if len({a["id"] for a in annotations}) != len(annotations):
        raise ValueError("重复审核题目ID")
    annotation_map = {a["id"]: a for a in annotations}
    old_reviews = {r["id"]: r for r in read_jsonl(source / "review_queue.jsonl")}
    records = []
    for old in read_jsonl(source / "cases.jsonl"):
        record = copy.deepcopy(old)
        path = source / old["trace_path"]
        if sha256_file(path) != old["trace_sha256"]:
            raise ValueError("实际Trace哈希不一致：" + old["id"])
        trace = read_json(path)
        if trace.get("result") != old["result"]:
            raise ValueError("结果与原Trace不一致")
        case = next(c for c in cases if c["id"] == old["id"])
        record.update(trace_path=str(path.resolve()), origin="saved_current_result", source_execution_status=old["execution_status"], execution_status="reused")
        record["scores"] = _current_score(case, old["result"], trace, rules, Path(manifest["config"]["source_root"]))
        if old["id"] in annotation_map:
            annotation = annotation_map[old["id"]]
            if annotation.get("sample") != old_reviews[old["id"]]["sample"]:
                raise ValueError("审核样本已被改写")
            scored = score_reviewed_labels(old_reviews[old["id"]]["sample"], annotation, manifest["rules_sha256"])
            record["scores"]["semantic"] = scored["metrics"]
            record["scores"]["review_provenance"] = {k: v for k, v in scored.items() if k != "metrics"}
            if scored.get("reviewer") and "context_labels" in annotation:
                record["scores"]["context_precision"] = context_precision(sent_contexts(trace), annotation["context_labels"])
            if scored["metrics"]["task_completion"].get("value") == 0:
                record["scores"]["quality_status"] = "failed_by_provided_review"
        records.append(record)
    unknown = set(annotation_map) - {r["id"] for r in records}
    if unknown:
        raise ValueError("审核题目不在本次结果内：" + str(sorted(unknown)))
    output.mkdir(parents=True)
    manifest.update(scoring_origin="rescore_saved_current_run", source_run=str(source.resolve()),
                    scoring_code_sha256=sha256_file(Path(__file__)), annotations_sha256=sha256_file(annotations_path) if annotations_path else None)
    write_json(output / "manifest.json", manifest)
    shutil.copyfile(source / "dataset.jsonl", output / "dataset.jsonl")
    shutil.copyfile(source / "rules.json", output / "rules.json")
    if annotations_path:
        shutil.copyfile(annotations_path, output / "provided_annotations.jsonl")
    summary = _save_current_report(output, records, _CurrentCapture({}, output, 0), cases, manifest)
    if annotations:
        queue = read_jsonl(output / "review_queue.jsonl")
        by_id = {r["id"]: r for r in records}
        for item in queue:
            if item["id"] in annotation_map:
                annotation = annotation_map[item["id"]]
                reviewed = by_id[item["id"]]["scores"]["semantic"]
                item.update(reviewer=annotation.get("reviewer"), labels=annotation.get("labels", {}),
                            status="partial_review" if any(m["value"] is not None for m in reviewed.values()) else "invalid_or_incomplete_review")
        write_jsonl(output / "review_queue.jsonl", queue)
    summary["source_generation_usage"] = read_json(source / "summary.json").get("provider_usage")
    write_json(output / "summary.json", summary)
    return summary


def compare_current(left: Path, right: Path):
    a, b = read_json(left / "manifest.json"), read_json(right / "manifest.json")
    required = ("dataset_sha256", "rules_sha256", "scoring_code_sha256")
    if any(not a.get(k) or a.get(k) != b.get(k) for k in required):
        return {"comparable": False, "reason": "题集、规则或评分代码不同/未固定，请先用同版score-current重评分"}
    rows_a = {r["id"]: r for r in read_jsonl(left / "cases.jsonl")}
    rows_b = {r["id"]: r for r in read_jsonl(right / "cases.jsonl")}
    if set(rows_a) != set(rows_b):
        return {"comparable": False, "reason": "题目集合不同"}
    deltas = {}
    for name in read_json(left / "summary.json")["metrics"]:
        metric_of = lambda r: r["scores"].get(name, r["scores"]["semantic"].get(name, metric()))
        ids_a = {i for i, r in rows_a.items() if metric_of(r)["value"] is not None}
        ids_b = {i for i, r in rows_b.items() if metric_of(r)["value"] is not None}
        if not ids_a or ids_a != ids_b:
            deltas[name] = metric(reason="没有相同的已评题集合", left_scored=len(ids_a), right_scored=len(ids_b))
        else:
            delta = sum(metric_of(rows_b[i])["value"] - metric_of(rows_a[i])["value"] for i in ids_a) / len(ids_a)
            deltas[name] = metric(delta, "evaluated", "同口径同已评集合的均值差；不外推到未评题", evaluated=len(ids_a))
    return {"comparable": True, "deltas": deltas, "runtime_profiles": {"left": {k:a[k] for k in ("code", "llm", "index_manifest", "config_sha256")},
            "right": {k:b[k] for k in ("code", "llm", "index_manifest", "config_sha256")}}}


def run_current(baseline: Path, output: Path, rules_path: Path | None = None, max_model_calls=40,
                reuse_current: Path | None = None, _retriever_factory=None, _query_runner=None):
    """在独立进程调用原问答链；无Judge，无自动重试，逐题先落Trace再落结果。"""
    import copy
    import os
    import shutil
    import uuid
    from urllib.parse import urlsplit
    from unittest.mock import patch
    from . import generation, pipeline
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if type(max_model_calls) is not int or not 1 <= max_model_calls <= 100:
        raise ValueError("必须设置1至100的明确模型调用上限")
    if output.exists():
        raise FileExistsError("不覆盖已存在的运行目录")
    cases = read_jsonl(baseline / "selected_cases.jsonl")
    if len(cases) != 35 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("本轮固定35题，ID必须唯一")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", c["id"]) for c in cases):
        raise ValueError("非法案例ID")
    rules_path = rules_path or ROOT / "eval/scoring_rules_v2.json"
    rules = read_json(rules_path)
    for case in cases:
        evidence_groups(case, rules)
    cfg, service = config(), generation.llm_config()
    data = Path(cfg["data_dir"])
    pointer = read_json(data / "index/current.json")
    build = data / "builds" / pointer["build_id"]
    index_manifest = read_json(build / "manifest.json")
    files = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256_file(p) for p in (ROOT / "doris_rag").glob("*.py")}
    manifest = {"run_id": datetime.now().strftime("%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:8],
        "origin": "current_live_run", "code": files, "config": cfg, "config_sha256": sha256_file(ROOT / "config.json"),
        "dataset_sha256": sha256_file(baseline / "selected_cases.jsonl"), "rules_sha256": sha256_file(rules_path),
        "index_manifest": index_manifest, "llm": {"host": urlsplit(service.get("base_url", "")).hostname,
        "model": service.get("model"), "temperature": 0.1, "generation_max_tokens": 6000, "extraction_max_tokens": 3500},
        "max_model_calls": max_model_calls, "judge_enabled": False, "automatic_retries": 0}
    output.mkdir(parents=True)
    write_json(output / "manifest.json", manifest)
    shutil.copyfile(rules_path, output / "rules.json")
    shutil.copyfile(baseline / "selected_cases.jsonl", output / "dataset.jsonl")
    for name in files:
        target = output / "snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    capture = _CurrentCapture(service, output, max_model_calls)
    reused = {}
    if reuse_current:
        previous = read_json(reuse_current / "manifest.json")
        keys = ("config_sha256", "dataset_sha256", "index_manifest", "llm")
        production = lambda m: {k: v for k, v in m["code"].items() if not Path(k).name.startswith("evaluation")}
        compatible = all(previous.get(k) == manifest[k] for k in keys) and production(previous) == production(manifest)
        write_json(output / "reuse_check.json", {"source": str(reuse_current.resolve()), "compatible": compatible})
        if compatible:
            old_dataset = {c["id"]: c for c in read_jsonl(reuse_current / "dataset.jsonl")}
            for old in read_jsonl(reuse_current / "cases.jsonl"):
                case = next((c for c in cases if c["id"] == old["id"]), None)
                path = reuse_current / old["trace_path"]
                if case and case == old_dataset.get(case["id"]) and old["execution_status"] in ("completed", "reused") and path.is_file():
                    trace = read_json(path)
                    sources = trace.get("generation_rows", [])
                    identity_ok = evidence_identity(sources, Path(cfg["source_root"]))["value"] == 1 if sources else not old["result"].get("evidence")
                    trace_ok = sha256_file(path) == old.get("trace_sha256", old.get("source_trace_sha256"))
                    if trace_ok and identity_ok and trace.get("result") == old["result"] and trace.get("status") == "completed":
                        reused[case["id"]] = (old, path)

    def reuse_record(case):
        old, source = reused[case["id"]]
        path = output / "traces" / (case["id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        trace = read_json(path)
        return {"id": case["id"], "run_id": manifest["run_id"], "source_run_id": old["run_id"], "origin": "reused_current_compatible",
                "question": case["question"], "version": case.get("version"), "execution_status": "reused", "result": old["result"],
                "trace_path": "traces/" + case["id"] + ".json", "source_trace_sha256": sha256_file(source),
                "trace_sha256": sha256_file(source),
                "scores": _current_score(case, old["result"], trace, rules, Path(cfg["source_root"]))}

    if len(reused) == len(cases):
        records = [reuse_record(case) for case in cases]
        return _save_current_report(output, records, capture, cases, manifest)
    started = time.perf_counter()
    try:
        if _retriever_factory is None:
            from .retrieval import Retriever
            _retriever_factory = Retriever
        retriever = _retriever_factory()
    except Exception as error:
        write_json(output / "initialization_error.json", {"type": type(error).__name__, "status": "blocked", "model_calls": 0})
        raise
    write_json(output / "load.json", {"seconds": time.perf_counter() - started, "build_id": retriever.manifest["build_id"]})
    original_rank, original_search = retriever._rank, retriever.search_plan
    original_enrich, original_answer = pipeline.enrich_context, pipeline.answer
    original_post = generation.requests.post

    def rank(*args, **kwargs):
        rows = original_rank(*args, **kwargs)
        capture.trace.setdefault("rankings", []).append({"query": args[0], "version": args[1],
            "rows": [{**copy.deepcopy(retriever.rows[r["index"]]), **copy.deepcopy(r)} for r in rows]})
        return rows

    def search(*args, **kwargs):
        rows = original_search(*args, **kwargs)
        capture.trace["retrieved_rows"] = copy.deepcopy(rows)
        return rows

    def enrich(rows, query):
        capture.trace["selected_raw_rows"] = copy.deepcopy(rows)
        enriched = original_enrich(rows, query)
        capture.trace["enriched_rows"] = copy.deepcopy(enriched)
        return enriched

    def answer(query, rows, *args, **kwargs):
        capture.trace["generation_rows"] = copy.deepcopy(rows)
        return original_answer(query, rows, *args, **kwargs)

    records = []
    with patch.object(retriever, "_rank", rank), patch.object(retriever, "search_plan", search), \
         patch.object(pipeline, "enrich_context", enrich), patch.object(pipeline, "answer", answer), \
         patch.object(generation.requests, "post", lambda *a, **k: capture.post(original_post, *a, **k)):
        for case in cases:
            drift = [name for name, digest in files.items() if sha256_file(ROOT / name) != digest]
            if drift or sha256_file(ROOT / "config.json") != manifest["config_sha256"]:
                raise RuntimeError("代码或配置在运行期间改变，停止后续调用")
            if generation.llm_config() != service:
                raise RuntimeError("模型配置在运行期间改变")
            if case["id"] in reused:
                records.append(reuse_record(case))
                _save_current_report(output, records, capture, cases, manifest)
                continue
            capture.trace = {"case_id": case["id"], "run_id": manifest["run_id"], "question": case["question"],
                             "version": case.get("version"), "top_k": cfg["top_k"], "model_calls": [], "status": "started"}
            capture.save()
            try:
                # Eval 已保存自己的逐题 Trace，不在正式运行目录重复落盘。
                from .telemetry import request_trace
                with request_trace(False, {'entrypoint': 'eval', 'run_id': manifest['run_id']}, persist=False):
                    result = (_query_runner or pipeline.run_query)(case["question"], case.get("version"), cfg["top_k"], cfg["retrieval_mode"], False, retriever)
                state = "completed"
            except Exception as error:
                result = {"status": "execution_failed", "answer": "未取得完整问答结果：" + type(error).__name__, "evidence": [], "error_type": type(error).__name__}
                state = "failed"
            capture.trace.update(status=state, result=copy.deepcopy(result))
            capture.save()
            record = {"id": case["id"], "run_id": manifest["run_id"], "origin": "current_live_run", "question": case["question"],
                "version": case.get("version"), "execution_status": state, "result": result,
                "trace_path": "traces/" + case["id"] + ".json", "scores": _current_score(case, result, capture.trace, rules, Path(cfg["source_root"]))}
            record["trace_sha256"] = sha256_file(output / record["trace_path"])
            records.append(record)
            _save_current_report(output, records, capture, cases, manifest)
            print(json.dumps({"case": case["id"], "status": result["status"], "model_calls": capture.network_calls,
                              "all_gold_hit": record["scores"]["pipeline_evidence_delivery"].get("all_hit")}, ensure_ascii=False), flush=True)
    return _save_current_report(output, records, capture, cases, manifest)


if __name__ == "__main__":
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Eval历史重放、已存结果评分及有界真实问答；默认离线，不运行Judge")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--run-current", action="store_true", help="先重放历史，再执行当前35题真实问答；不运行Judge")
    parser.add_argument("--max-model-calls", type=int, default=40)
    parser.add_argument("--reuse-current", type=Path, help="仅复用输入、生产代码、模型配置及索引完全匹配的已完成答案")
    parser.add_argument("--score-current", type=Path, help="对已保存的当前运行重新评分，不调用模型")
    parser.add_argument("--annotations", type=Path, help="审核人提供的JSONL标注，仅用于score-current")
    parser.add_argument("--compare-current", type=Path, help="与另一个已评分目录进行同口径比较，不调用模型")
    args = parser.parse_args()
    if (args.annotations or args.compare_current) and not args.score_current:
        parser.error("annotations/compare-current仅用于score-current，不会自动启动Judge")
    if args.score_current and args.rules:
        parser.error("score-current使用源运行固化的rules.json，不接受静默替换规则")
    if args.score_current:
        if args.run_current:
            parser.error("score-current与run-current不可同时使用")
        result = rescore_current(args.score_current, args.output, args.annotations)
        if args.compare_current:
            write_json(args.output / "comparison.json", compare_current(args.compare_current, args.output))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.run_current:
        args.output.mkdir(parents=True, exist_ok=False)
        replay(args.baseline, args.output / "historical", args.rules)
        print(json.dumps(run_current(args.baseline, args.output / "current", args.rules, args.max_model_calls, args.reuse_current), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(replay(args.baseline, args.output, args.rules), ensure_ascii=False, indent=2))
