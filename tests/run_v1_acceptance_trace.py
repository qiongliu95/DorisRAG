"""补跑原卡缺测输入；独立 HTTP 进程记录实际模型请求，不修改产品链路。"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ACTIVE = contextvars.ContextVar("acceptance_call", default=None)


def stamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve(item):
    if "input" in item:
        return item["input"]
    if "input_ref" not in item:
        return None
    ref = item["input_ref"]
    path = ROOT / ref["file"]
    values = ([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
              if path.suffix == ".jsonl" else read(path))
    if isinstance(values, dict):
        values = values["questions"]
    return next(row[ref["field"]] for row in values if row["id"] == ref["id"])


def code_hashes():
    files = list((ROOT / "doris_rag").glob("*.py")) + list((ROOT / "ui").glob("*"))
    files += list((ROOT / "tests").glob("*.py"))
    files += list((ROOT / "tests/fixtures").glob("*.json"))
    files += [ROOT / "config.json", ROOT / "requirements-lock.txt", Path(__file__).resolve(),
              ROOT / "eval/v1_final_acceptance_20261024.json", ROOT / "reports/V1_PRODUCT_CONTRACT_20261024.md"]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p) for p in files if p.is_file()}


def assert_snapshot(manifest):
    changed = [name for name, sha in manifest["files"].items() if digest(ROOT / name) != sha]
    if changed:
        raise RuntimeError("验收期间实施或案例变化：" + ", ".join(changed))


def observe_post(original, trace, redact, budget=None):
    """只旁路记录正文与状态，不保存认证头；原调用及异常保持原样。"""
    def post(url, *args, **kwargs):
        active = ACTIVE.get()
        if active is None or not str(url).endswith("/chat/completions"):
            return original(url, *args, **kwargs)
        if budget is not None:
            if budget['remaining']<=0:
                raise RuntimeError('acceptance_model_call_budget')
            budget['remaining']-=1
        call = {"started_at": stamp(), "request": json.loads(redact(json.dumps(kwargs.get("json"), ensure_ascii=False))), "status": "started"}
        active["model_calls"].append(call)
        from doris_rag.telemetry import observe_exchange
        return observe_exchange(original, url, args, kwargs, call,
            lambda: save(trace / (active["message_id"] + ".json"), active),
            lambda value: redact(value) if isinstance(value, str) else json.loads(redact(json.dumps(value, ensure_ascii=False))))
    return post


def serve(output, port):
    from doris_rag import generation, web
    from doris_rag.sessions import SessionStore
    import uvicorn
    manifest = read(output / "run_manifest.json")
    cfg = generation.llm_config()
    secret = cfg["api_key"]
    redact = lambda value: value.replace(secret, "[REDACTED]") if secret else value
    web.store = SessionStore(output / "sessions.sqlite3")
    original_turn = web.complete_turn
    original_post = generation.requests.post
    budget = {'remaining':manifest['max_model_calls']} if manifest.get('conversation_id') else None
    generation.requests.post = observe_post(original_post, output / "turn_traces", redact, budget)

    def traced_turn(database, sid, mid, turn, started, feedback_seconds):
        trace = {"run_id": manifest["run_id"], "session_id": sid, "message_id": mid,
                 "query_pipeline_input": turn, "model_calls": [], "started_at": stamp()}
        token = ACTIVE.set(trace)
        try:
            assert_snapshot(manifest)
            from doris_rag.telemetry import request_trace
            with request_trace(bool(manifest.get('conversation_id')), {'entrypoint': 'acceptance', 'session_id': sid, 'message_id': mid}, persist=False) as native:
                original_turn(database, sid, mid, turn, started, feedback_seconds)
            if manifest.get('conversation_id'):
                save(output / 'native_traces' / (mid+'.json'), native.data)
            trace["status"] = "finished"
        except Exception as error:
            trace.update(status="capture_failed", error_type=type(error).__name__)
            database.finish(sid, mid, {"status": "execution_failed", "answer": "验收实施快照变化或记录失败。", "evidence": []})
        finally:
            trace["completed_at"] = stamp()
            save(output / "turn_traces" / (mid + ".json"), trace)
            ACTIVE.reset(token)
    web.complete_turn = traced_turn
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning")


def conversation_tasks(conversation_id, defect_suite=False):
    asset=next(c for c in read(ROOT/'tests/fixtures/session_relation_cases.json') if c['id']==conversation_id)
    tasks=[{'card':conversation_id,'input_index':n,'question':text,'session_key':conversation_id}
           for n,text in enumerate(asset['inputs'])]
    if defect_suite:
        defects=read(ROOT/'tests/fixtures/serious_defects_cases.json')
        tasks += [{'card':'D02-A18','input_index':n,'question':item['input'],'session_key':'D02-A18'}
                  for n,item in enumerate(defects['D02']['original_conversation'])]
        tasks += [{'card':'D03-A12_1','input_index':0,'question':defects['D03']['input']}]
        tasks += [{'card':c['id'],'input_index':0,'question':c['question']} for c in defects['D05']]
    return tasks


def execute(output, port, conversation_id=None, defect_suite=False):
    import requests
    from urllib.parse import urlsplit
    from doris_rag.common import config
    from doris_rag.generation import llm_config
    output.mkdir(parents=True, exist_ok=False)
    cfg = config()
    data = Path(cfg["data_dir"])
    pointer = read(data / "index/current.json")
    build = data / "builds" / pointer["build_id"]
    index = read(build / "manifest.json")
    llm = llm_config()
    endpoint = urlsplit(llm["base_url"])
    manifest = {"run_id": str(uuid.uuid4()), "started_at": stamp(), "files": code_hashes(),
                'conversation_id':conversation_id, 'defect_suite':defect_suite,
                'max_model_calls':44 if defect_suite else 18 if conversation_id else None,
                "index_manifest": index, "config": cfg,
                "llm": {"model": llm["model"], "endpoint": endpoint.scheme + "://" + str(endpoint.hostname) + endpoint.path,
                        "temperature": .1, "generation_max_tokens": 6000, "configured": all(llm.values())},
                "historical_run_provenance": "历史记录只按其原有证据复用，不追溯补写当时提示词、响应或代码身份。"}
    for name in manifest["files"]:
        target = output / "snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    checks = {}
    for filename, field in [("faiss.index", "index_sha256"), ("chunk_metadata.json", "metadata_sha256"),
                            ("vectors.npy", "vectors_sha256")]:
        checks[filename] = digest(build / filename) == index[field]
    for filename, field in [("chunks.jsonl", "chunks_sha256"), ("documents.jsonl", "documents_sha256")]:
        checks[filename] = digest(data / "processed" / filename) == index[field]
    manifest["artifact_checks"] = checks
    save(output / "run_manifest.json", manifest)
    if not checks or not all(checks.values()):
        raise RuntimeError("活动构建产物校验失败")
    prior_files = [ROOT / "reports/v1_integration_20261024/full_http_cases.jsonl"]
    prior_files += list((ROOT / "reports/v1_system_acceptance_20261024").glob("*cases.jsonl"))
    previous = []
    archived = {}
    for path in sorted(prior_files):
        target = output / "reused" / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        archived[str(path.relative_to(ROOT))] = digest(target)
        previous.extend(json.loads(line) for line in target.read_text(encoding="utf-8").splitlines())
    save(output / "reused_manifest.json", archived)
    catalog = read(ROOT / "eval/v1_final_acceptance_20261024.json")
    tasks = []
    for card in catalog["cases"]:
        if card["mode"] not in ("single", "variants"):
            continue
        for i, item in enumerate(card["inputs"]):
            query = resolve(item)
            # 连续会话不能随意拆成独立问题；此处只补原卡独立输入的缺测项。
            if not any(r.get("question", r.get("query")) == query for r in previous):
                tasks.append({"card": card["id"], "input_index": i, "question": query})
    if conversation_id:
        tasks=conversation_tasks(conversation_id,defect_suite)
    save(output / "planned_missing_inputs.json", tasks)
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    process = None
    with (output / "server.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--serve", "--output", str(output), "--port", str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            http = requests.Session()
            http.trust_env = False
            begin = time.monotonic()
            while True:
                if process.poll() is not None:
                    raise RuntimeError("独立验收服务退出，见 server.log")
                try:
                    state = http.get(base + "/api/status", timeout=10).json()
                    if state.get("retrieval_ready"):
                        break
                    if state.get("warmup_status") == "failed":
                        raise RuntimeError("检索预热失败")
                except requests.RequestException:
                    pass
                if time.monotonic() - begin > 600:
                    raise TimeoutError("独立验收预热超时")
                time.sleep(1)
            save(output / "service_status.json", state)
            sessions = {}
            for task in tasks:
                assert_snapshot(manifest)
                key=task.get('session_key')
                if key and key in sessions:
                    sid=sessions[key]
                else:
                    sid = http.post(base + "/api/sessions", timeout=30).json()["id"]
                    if key:sessions[key]=sid
                before=http.get(base+'/api/sessions/'+sid,timeout=30).json()
                started = time.perf_counter()
                response = http.post(base + f"/api/sessions/{sid}/turns", json={"query": task["question"], "revision": before['revision']}, timeout=30)
                response.raise_for_status()
                accepted = response.json()
                feedback = time.perf_counter() - started
                deadline = time.monotonic() + 420
                while True:
                    doc = http.get(base + "/api/sessions/" + sid, timeout=30).json()
                    if not doc["busy"]:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("验收请求仍在执行，不自动重发")
                    time.sleep(.25)
                row = {**task, "run_id": manifest["run_id"], "session_id": sid, "message_id": accepted["message_id"],
                       "result": doc["messages"][-1]["result"], "state_after": doc["state"],
                       'state_before':before['state'], 'message_count':len(doc['messages']),
                       'native_trace':str(output/'native_traces'/(accepted['message_id']+'.json')) if conversation_id else None,
                       "client_first_feedback_seconds": feedback, "client_complete_seconds": time.perf_counter() - started,
                       "transport": "actual_loopback_http_isolated_process", "semantic_review": "pending"}
                with (output / "new_cases.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(task["card"], task["input_index"], row["result"]["status"], flush=True)
            save(output / "execution_status.json", {"phase": "completed", "new_requests": len(tasks), "at": stamp(), "v1_accepted": False})
        except Exception as error:
            save(output / "execution_status.json", {"phase": "failed", "error_type": type(error).__name__, "at": stamp(), "v1_accepted": False})
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
            save(output / "snapshot_after.json", {"files": code_hashes(), "matches_start": code_hashes() == manifest["files"]})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument('--conversation-id',help='顺序执行tests/fixtures/session_relation_cases.json中的一条完整会话')
    parser.add_argument('--defect-suite',action='store_true',help='同批执行冻结D02/D03/D05专项；最多44次模型请求')
    args = parser.parse_args()
    if args.serve:
        serve(args.output.resolve(),args.port)
    else:
        if args.defect_suite and not args.conversation_id:
            parser.error('--defect-suite需要--conversation-id')
        execute(args.output.resolve(),args.port,args.conversation_id,args.defect_suite)
