"""对已启动的本地服务做真实 HTTP 验证；不会调用 LLM。"""
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8765"


def main():
    session = requests.Session()
    session.trust_env = False
    checks = {}
    page = session.get(BASE, timeout=10)
    checks["page"] = page.status_code == 200 and "DorisRAG" in page.text
    status = session.get(BASE + "/api/status", timeout=10).json()
    checks["active_index"] = status["index_ready"] is True
    checks["shared_defaults"] = status["default_mode"] == "hybrid_precise" and status["default_version"] == "4.x"
    results = {}

    def ask(name, query, **kwargs):
        response = session.post(BASE + "/api/ask", json={"query": query, "evidence_only": True, **kwargs}, timeout=180)
        response.raise_for_status()
        result = response.json()
        results[name] = result
        return result

    short = ask("short_document", "PI() 返回什么？")
    checks["short_document_retrievable"] = any(row["relative_path"].endswith("/pi.md") for row in short["evidence"])
    checks["shared_retrieval_mode"] = all(row["retrieval_mode"] == status["default_mode"] for row in short["evidence"])
    for version in ("2.1", "3.x", "4.x"):
        result = ask(version, "max_routine_load_task_concurrent_num 默认值是多少？", version=version)
        checks["scope_" + version] = bool(result["evidence"]) and all(row["source_version"] == version for row in result["evidence"])
    comparison = ask("comparison", "Doris2.1 和3.x的 max_routine_load_task_concurrent_num 默认值有什么变化？", version="all")
    checks["both_explicit_version_families"] = {row["source_version"] for row in comparison["evidence"]} == {"2.1", "3.x"}
    upload = ask("patch_notice", "Upload Action POST 用法", version="4.x")
    checks["patch_notice_retained"] = any("4.1.4" in " ".join(row["document_version_notices"]) for row in upload["evidence"])
    source = session.get(BASE + "/api/source", params={"doc_id": short["evidence"][0]["doc_id"]}, timeout=10)
    checks["original_source_access"] = source.status_code == 200 and "PI()" in source.text
    checks["unknown_source_rejected"] = session.get(BASE + "/api/source", params={"doc_id": "../../.env"}, timeout=10).status_code == 404
    checks["empty_question_rejected"] = session.post(BASE + "/api/ask", json={"query": ""}, timeout=10).status_code == 422
    checks["blank_question_rejected"] = session.post(BASE + "/api/ask", json={"query": "   "}, timeout=10).status_code == 400
    checks["unknown_version_rejected"] = session.post(BASE + "/api/ask", json={"query": "PI()", "version": "5.x"}, timeout=10).status_code == 400
    if not status["llm_configured"]:
        response = session.post(BASE + "/api/ask", json={"query": "PI() 返回什么？"}, timeout=60)
        results["missing_llm"] = response.json()
        checks["missing_llm_is_explicit"] = response.status_code == 200 and response.json()["status"] == "llm_not_configured"
    report = {"passed": all(checks.values()), "checks": checks, "status": status, "results": results}
    (ROOT / "reports" / "http_smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": checks}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
