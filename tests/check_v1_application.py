"""V1 实际 HTTP/LLM 验证。组件模式不加载 Dense，绝不冒充全库验收。"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import requests
import uvicorn

from doris_rag.common import write_json, write_jsonl

REPORT = ROOT / "reports/v1_integration_20261024"


def ready():
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            return {}
    from doris_rag.common import config
    return {"round1_completed": read(ROOT / "reports/round1_20261023/run_status.json").get("phase") == "completed",
            "round1_integrity_passed": read(ROOT / "reports/round1_20261023/build_checks.json").get("passed", False),
            "new_pointer": (Path(config()["data_dir"]) / "index/current.json").exists(),
            "round2_executed": read(ROOT / "reports/round2_20261023/full_e2e_summary.json").get("executed", False)}


def validate(base, component=False, groups=None):
    http = requests.Session()
    http.trust_env = False
    records, sessions = [], {}

    def ask(group, question, expected_versions=None, expected_kind=None, expected_status=None):
        if groups and group not in groups:
            return
        if group not in sessions:
            response = http.post(base + "/api/sessions", timeout=20)
            response.raise_for_status(); sessions[group] = response.json()
        doc = sessions[group]
        started = time.perf_counter()
        response = http.post(base + f"/api/sessions/{doc['id']}/turns", json={"query": question, "revision": doc["revision"]}, timeout=30)
        response.raise_for_status()
        feedback = time.perf_counter() - started
        deadline = time.monotonic() + 360
        while True:
            response = http.get(base + f"/api/sessions/{doc['id']}", timeout=30)
            response.raise_for_status(); completed = response.json()
            if not completed["busy"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("本次 HTTP 任务在预算内未完成；不自动重发")
            time.sleep(.25)
        sessions[group] = completed
        message = completed["messages"][-1]
        result = message.get("result", {})
        actual = result.get("understanding", {}).get("versions", [])
        checks = {"http_completed": True, "expected_status": message["status"] in (expected_status or ["generated"])}
        if expected_versions is not None:
            checks["scope"] = set(actual) == set(expected_versions)
            checks["evidence_scope"] = all(r["source_version"] in expected_versions for r in result.get("evidence", []))
        if expected_kind:
            checks["turn_kind"] = result["turn_kind"] == expected_kind
        if message["status"] == "generated":
            checks["citation_and_structure"] = result.get("citations_valid") is True and result.get("answer_structure_valid") is True
        records.append({"question": question, "group": group, "checks": checks, "result": result,
                        "client_first_feedback_seconds": feedback, "client_complete_seconds": time.perf_counter()-started,
                        "semantic_review": "required", "state_after": completed["state"]})
        prefix = ("component_" + "_".join(groups)) if component and groups else "component" if component else "full_http"
        write_jsonl(REPORT / (prefix + "_cases.jsonl"), records)
        print(group, message["status"], round(records[-1]["client_complete_seconds"], 2), flush=True)

    ask("function", "PI() 返回什么？请给出 SQL 示例。", ["4.x"], "new_question")
    ask("function", "那这个值能用于什么计算？", ["4.x"], "follow_up")
    ask("config", "Doris 3.x 的 max_routine_load_task_num_per_be 默认值是多少？", ["3.x"])
    ask("config", "纠正一下，我用的是 Doris 2.1.4", ["2.1"], "correction")
    ask("config", "那比较 3.x 和4.x 的这个参数默认值。", ["3.x", "4.x"], "follow_up")
    ask("config", "那具体参数含义呢？", ["2.1"], "follow_up")
    ask("config", "换个问题，PI() 返回什么？", ["4.x"], "topic_switch")
    ask("all", "请查所有版本，PI() 返回什么？", ["4.x", "3.x", "2.1"])
    ask("clarify", "帮我看看", expected_status=["needs_clarification"])
    ask("clarify", "我想知道 PI() 返回什么？", ["4.x"], "clarification_reply")
    ask("unknown", "quantum_auto_fix_mode 默认值和开启方法是什么？", ["4.x"], expected_status=["insufficient_evidence", "no_evidence"])
    ask("log", "Doris 4.1.4 升级后接口失败，日志如下：\nERROR: Upload Action POST /api/db/table/upload HTTP 404\n请解释文档中的版本条件，不能直接认定集群根因。", ["4.x"])
    ask("log", "那下一步还需要确认什么？", ["4.x"], "follow_up")
    for group, doc in sessions.items():
        restored = http.get(base + f"/api/sessions/{doc['id']}", timeout=30).json()
        assert restored["messages"] == doc["messages"], "历史恢复不一致"
    durations = [r["client_complete_seconds"] for r in records]
    feedbacks = [r["client_first_feedback_seconds"] for r in records]
    summary = {"component_only": component, "executed": True, "cases": len(records),
               "execution_passed_cases": sum(all(r["checks"].values()) for r in records),
               "statuses": dict(Counter(r["result"]["status"] for r in records)),
               "client_complete_p50_seconds": float(np.percentile(durations,50)),
               "client_complete_p95_seconds": float(np.percentile(durations,95)),
               "client_feedback_p50_seconds": float(np.percentile(feedbacks,50)),
               "client_feedback_p95_seconds": float(np.percentile(feedbacks,95)),
               "semantic_acceptance": "requires_review", "v1_accepted": False,
               "conditions": "本机后台 CPU 索引构建同时运行" if component else "记录冷启动和已加载请求；本机实测，无固定 SLA",
               "limits": "指定来源 BM25/完整串/RRF + 真 LLM + 真 HTTP；没有 Dense，不能用于全库召回或正式性能结论" if component else
                         "全量新索引 HTTP/LLM/Dense 实测，结构检查不等于语义正确率",
               "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat()}
    prefix = ("component_" + "_".join(groups)) if component and groups else "component" if component else "full_http"
    write_json(REPORT / (prefix + "_summary.json"), summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--groups", nargs="+")
    args = parser.parse_args()
    REPORT.mkdir(exist_ok=True)
    if not args.component:
        while not all(ready().values()):
            state = ready()
            write_json(REPORT / "full_http_summary.json", {"status":"waiting_for_index_and_round2", "executed":False,
                       "v1_accepted":False, "gates":state})
            if not args.wait:
                print(json.dumps(state));raise SystemExit(2)
            time.sleep(60)
        http=requests.Session();http.trust_env=False
        warm_started=time.perf_counter()
        while True:
            status=http.get('http://127.0.0.1:8765/api/status',timeout=30).json()
            if status.get('warmup_status')=='failed':raise RuntimeError('生产检索预热失败')
            if status.get('retrieval_ready'):break
            if time.perf_counter()-warm_started>900:raise TimeoutError('预热在预算内未完成')
            time.sleep(2)
        write_json(REPORT/'full_warmup.json',{'observed_wait_seconds':time.perf_counter()-warm_started,'warmup':status['warmup']})
        summary=validate("http://127.0.0.1:8765")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if summary['execution_passed_cases']!=summary['cases']:raise SystemExit(1)
    else:
        # 独立进程中的组件夹具；不修改生产配置、活动指针或生产会话库。
        from check_query_application import subset_retriever
        from doris_rag import web
        from doris_rag.pipeline import run_query
        from doris_rag.sessions import SessionStore
        fixture = subset_retriever()
        web.index_ready = lambda: True
        def component_query(query, version=None, **kwargs):
            kwargs.pop("retriever_factory", None)
            return run_query(query, version, mode="lexical", retriever=fixture, **kwargs)
        web.run_query = component_query
        with tempfile.TemporaryDirectory() as temp:
            web.store = SessionStore(Path(temp)/"component_sessions.db")
            server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=8766, log_level="error"))
            thread = threading.Thread(target=server.run, daemon=True);thread.start()
            while not server.started:
                if not thread.is_alive():raise RuntimeError("组件 HTTP 服务未启动")
                time.sleep(.1)
            try:
                summary=validate("http://127.0.0.1:8766", True, args.groups)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if summary['execution_passed_cases']!=summary['cases']:raise SystemExit(1)
            finally:
                server.should_exit=True;thread.join(timeout=15)
                web.workers.shutdown(wait=True)
