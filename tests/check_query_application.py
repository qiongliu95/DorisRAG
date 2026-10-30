"""代表性真实服务联调；指定来源组件检查与全量 Dense 端到端检查分别记录。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doris_rag.common import config, read_jsonl, sha256_file, write_json, write_jsonl
from doris_rag.pipeline import run_query
from doris_rag.retrieval import Retriever

REPORT = ROOT / "reports/round2_20261023"


def execution_checks(execution, case):
    checks = {"expected_status": execution["status"] == case.get("expected_status", "generated")}
    if execution["status"] == "generated":
        checks["citation_numbers_valid"] = execution["citations_valid"]
        checks["fact_sections_present"] = execution["answer_structure_valid"]
    if execution["evidence"]:
        checks["unique_chunk_ids"] = len({r["chunk_id"] for r in execution["evidence"]}) == len(execution["evidence"])
        checks["provenance_present"] = all(bool(r["retrieval_trace"]) for r in execution["evidence"])
    return checks


def build_state():
    data = Path(config()["data_dir"])
    pointer = data / "index/current.json"
    status = ROOT / "reports/round1_20261023/run_status.json"
    state = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {}
    checks = ROOT / "reports/round1_20261023/build_checks.json"
    return {"pointer_exists": pointer.exists(), "round1_status": state.get("phase"),
            "round1_checks_passed": checks.exists() and json.loads(checks.read_text(encoding="utf-8")).get("passed", False)}


def subset_retriever():
    selected = []
    paths = ("admin-manual/open-api/fe-http/upload-action.md",
             "sql-manual/sql-functions/scalar-functions/numeric-functions/pi.md")
    path = Path(config()["data_dir"]) / "processed/chunks.jsonl"
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        relative = row["relative_path"]
        keep = relative in paths
        keep |= relative == "admin-manual/config/fe-config.md" and "max_routine_load_task_num_per_be" in row["section_path"]
        keep |= relative == "lakehouse/catalogs/hive-catalog.mdx" and "hive.recursive_directories" in row["chunk_text"]
        keep |= relative.endswith("variant-workload-guide.md") and any(t in row["chunk_text"] for t in ("DOC mode", "Sparse"))
        if keep:
            selected.append(row)
    retrieval = Retriever.__new__(Retriever)
    retrieval.rows, retrieval.lexical = selected, None
    retrieval.manifest = {"build_id": "component-only-no-vectors:" + sha256_file(path)[:12], "query_prefix": ""}
    # 测试夹具仅提供指定来源的 BM25/完整串路径，绝不伪造或读取未完成向量。
    retrieval.candidates = lambda version: (None, [i for i, row in enumerate(selected) if row["source_version"] == version])
    return retrieval


def run(component=False):
    state = build_state()
    name = "component" if component else "full_e2e"
    if not component and not (state["pointer_exists"] and state["round1_status"] == "completed" and state["round1_checks_passed"]):
        write_json(REPORT / "full_e2e_summary.json", {"status": "blocked_by_round1_build", "build_state": state,
                   "end_to_end_passed": False, "executed": False})
        return False
    retrieval = subset_retriever() if component else Retriever()
    cases = read_jsonl(ROOT / "eval/round2_cases.jsonl")
    results = []
    for case in cases:
        started = time.time()
        try:
            execution = run_query(case["question"], case["version"], 6,
                                  "lexical" if component else "hybrid_precise", False, retrieval)
            checks = execution_checks(execution, case)
            result = {**case, "execution": execution, "execution_checks": checks,
                      "seconds": round(time.time() - started, 3), "semantic_review": "required",
                      "note": "引用编号及格式检查不能证明实质结论正确或忠实。"}
        except Exception as error:
            result = {**case, "execution": {"status": "service_or_pipeline_error", "error_type": type(error).__name__},
                      "execution_checks": {"execution_completed": False}, "semantic_review": "not_executed"}
        results.append(result)
        write_jsonl(REPORT / (name + "_cases.jsonl"), results)
        print(case["id"], result["execution"]["status"], flush=True)
    summary = {"status": "executed", "executed": True, "component_only": component,
        "end_to_end_passed": False, "semantic_acceptance": "requires_review",
        "cases": len(results), "generation_statuses": dict(Counter(r["execution"]["status"] for r in results)),
        "execution_checks_passed_cases": sum(all(r["execution_checks"].values()) for r in results),
        "build_id": retrieval.manifest["build_id"], "build_state": state,
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "limits": ["组件模式只在指定来源片段上走 BM25/完整串/RRF 和真实 LLM，不能用于全库召回或 Dense 结论。" if component else
                   "使用完成并通过第一轮验收的全量新索引；语义正确性仍须逐项复核。",
                   "不使用独立 Judge，不将结构/引用检查作为语义分数。"]}
    write_json(REPORT / (name + "_summary.json"), summary)
    return True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if args.wait:
        write_json(REPORT / "full_e2e_summary.json", {"status": "waiting_for_round1_build", "executed": False,
                   "end_to_end_passed": False, "build_state": build_state()})
        while not (build_state()["pointer_exists"] and build_state()["round1_status"] == "completed" and build_state()["round1_checks_passed"]):
            if build_state()["round1_status"] == "failed":
                write_json(REPORT / "full_e2e_summary.json", {"status": "round1_build_failed", "executed": False,
                           "end_to_end_passed": False, "build_state": build_state()})
                raise SystemExit(1)
            time.sleep(60)
    run(args.component)
