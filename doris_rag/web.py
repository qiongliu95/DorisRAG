from __future__ import annotations

import json
import threading
import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import wraps
from time import perf_counter

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .common import ROOT, config, read_jsonl, sha256_file
from .generation import llm_config
from .pipeline import run_query
from .sessions import SessionStore, SessionBusy, VERSION_NOTICE, turn_task
from .presentation import present_result, present_session, failure_message
from .telemetry import current_trace, request_trace, traced, stage

app = FastAPI(title="Doris 文档问答")
lock = threading.Lock()
retriever = None
store = None
store_lock = threading.Lock()
workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="doris-chat")
# 两个执行位加两个等待位；会话与兼容接口共用，避免长请求形成无界队列。
request_slots=threading.BoundedSemaphore(4)
warm_lock = threading.Lock()
warm_future = None


@traced('ingestion.index_gate')
def index_ready():
    data = Path(config()["data_dir"])
    try:
        checks = json.loads((ROOT / "reports/round1_20261023/build_checks.json").read_text(encoding="utf-8"))
        state = json.loads((ROOT / "reports/round1_20261023/run_status.json").read_text(encoding="utf-8"))
        active = json.loads((data / "index/current.json").read_text(encoding="utf-8"))["build_id"]
        return (state.get('phase')=='completed' and checks.get('passed') is True
                and checks.get('build_id')==active and state.get('build_id')==active)
    except (OSError, ValueError, KeyError):
        return False


def warm_retrieval():
    started = perf_counter()
    active = load_retriever()
    with active.search_lock:
        if active.lexical is None:
            from .lexical import LexicalIndex
            active.lexical = LexicalIndex(active.rows)
        active.candidates("4.x")
    return {"build_id": active.manifest["build_id"], "seconds": round(perf_counter() - started, 3)}


def session_store():
    global store
    with store_lock:
        if store is None:
            store = SessionStore(ROOT / "data" / "sessions.sqlite3")
        return store


@traced('ingestion.index_load')
def load_retriever():
    global retriever
    if not index_ready():
        raise FileNotFoundError("新索引尚未通过构建后验收")
    with lock:
        pointer = Path(config()["data_dir"]) / "index" / "current.json"
        active = json.loads(pointer.read_text(encoding="utf-8"))["build_id"]
        if retriever is None or retriever.manifest["build_id"] != active:
            from .retrieval import Retriever
            retriever = Retriever()
        return retriever


class Question(BaseModel):
    query: str = Field(min_length=1, max_length=32000)
    version: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=15)
    mode: str | None = None
    evidence_only: bool = False
    diagnostic: bool = False


class Turn(BaseModel):
    query: str = Field(min_length=1, max_length=32000)
    revision: int | None = Field(default=None, ge=0)
    diagnostic: bool = False


class RetryTurn(BaseModel):
    revision: int = Field(ge=0)
    diagnostic: bool = False


class SessionTitle(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=0)


def execution_failure(error):
    import requests
    kind = type(error).__name__
    for cls in (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.SSLError,
                requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        if isinstance(error, cls):
            kind = cls.__name__
            break
    code = getattr(getattr(error, 'response', None), 'status_code', None)
    return {'status':'execution_failed', 'answer':failure_message(kind, code), 'error_type':kind,
            'http_status':code, 'evidence':[]}


def trace_question(function):
    @wraps(function)
    def run(question: Question):
        with request_trace(question.diagnostic, {'entrypoint': 'http.ask'}) as trace:
            trace.event('http.question', question.model_dump())
            try:
                result = function(question)
            except HTTPException as error:
                trace.fail(type(error).__name__)
                error.headers = {**(error.headers or {}), 'X-DorisRAG-Trace-ID': trace.data['request_id']}
                trace.finish({'status': 'execution_failed', 'http_status': error.status_code})
                if trace.summary()['trace_path']:
                    error.headers['X-DorisRAG-Trace-Path'] = trace.summary()['trace_path']
                raise
            trace.finish(result)
            result['diagnostics'] = trace.summary()
            return result
    return run


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


@app.get("/app.js")
def javascript():
    return PlainTextResponse((ROOT / "ui" / "app.js").read_text(encoding="utf-8"), media_type="text/javascript")


@app.get("/api/status")
def status():
    global warm_future
    data = Path(config()["data_dir"])
    ready = index_ready()
    if ready:
        # 模型、BM25 及默认版本候选只预热一次；不让首条提问承担依赖加载和建缓存成本。
        with warm_lock:
            active=json.loads((data/'index/current.json').read_text(encoding='utf-8'))['build_id']
            if (warm_future is not None and warm_future.done() and warm_future.exception() is None
                    and warm_future.result().get('build_id')!=active):
                warm_future=None
            if warm_future is None:
                warm_future = workers.submit(warm_retrieval)
    warmed = warm_future is not None and warm_future.done() and warm_future.exception() is None
    warm_failed = warm_future is not None and warm_future.done() and warm_future.exception() is not None
    audit_path = data / "manifests" / "chunk_audit.json"
    return {"index_ready": ready, "llm_configured": all(llm_config().values()),
            "default_version": "4.x", "default_mode": config()["retrieval_mode"],
            "audit": json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else None,
            "notice": VERSION_NOTICE, "retriever_loaded": retriever is not None,
            "retrieval_ready": warmed, "warmup": warm_future.result() if warmed else None,
            "warmup_status": "failed" if warm_failed else "ready" if warmed else "warming" if ready else "not_started"}


@app.post("/api/ask")
@trace_question
def ask(question: Question):
    global retriever
    slots=request_slots
    if not slots.acquire(blocking=False):
        raise HTTPException(503,'当前处理任务已满，请稍后重试；本次未提交。')
    try:
        cfg = config()
        version = question.version
        top_k = question.top_k or cfg["top_k"]
        mode = question.mode or cfg["retrieval_mode"]
        active_retriever = load_retriever()
        return present_result(run_query(question.query, version, top_k, mode, question.evidence_only, active_retriever))
    except FileNotFoundError:
        raise HTTPException(503, "知识库尚未准备好，请稍后重试。")
    except ValueError as error:
        raise HTTPException(400, '本次材料未能完成处理，请核对问题或稍后重试。')
    except Exception as error:
        # 不把模型服务 URL、认证头或原始响应回传给浏览器。
        raise HTTPException(502, execution_failure(error)['answer'])
    finally:
        slots.release()


@app.get("/api/sessions")
def list_sessions():
    return session_store().list()


@app.post("/api/sessions", status_code=201)
def create_session():
    return session_store().create()


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    try:
        return present_session(session_store().get(sid))
    except KeyError:
        raise HTTPException(404, "会话不存在或已删除")


@app.delete("/api/sessions/{sid}", status_code=204)
def delete_session(sid: str):
    try:
        session_store().delete(sid)
    except KeyError:
        raise HTTPException(404, "会话不存在或已删除")


@app.patch('/api/sessions/{sid}')
def rename_session(sid: str, body: SessionTitle):
    try:
        return present_session(session_store().rename(sid, body.title, body.revision))
    except KeyError:
        raise HTTPException(404,'会话不存在或已删除')
    except SessionBusy as error:
        raise HTTPException(409,str(error))
    except ValueError as error:
        raise HTTPException(400,str(error))


def complete_turn(database, sid, mid, turn, started, feedback_seconds):
    inherited = current_trace.get()
    from contextlib import nullcontext
    scope = nullcontext(inherited) if inherited else request_trace(turn.get('diagnostic', False),
        {'entrypoint': 'http.session', 'session_id': sid, 'message_id': mid})
    with scope as trace:
        trace.event('session.turn', turn)
        result = _complete_turn(database, sid, mid, turn, started, feedback_seconds)
        trace.finish(result or {'status': 'cancelled_before_execution'})


def _complete_turn(database, sid, mid, turn, started, feedback_seconds):
    try:
        # 排队期间可能已删除会话；不能为已失效的任务继续产生模型费用。
        try:
            doc=database.get(sid)
        except KeyError:
            return
        if not doc['busy'] or doc['messages'][-1]['id']!=mid:
            return
        if turn.get('attempt_id') and doc['messages'][-1].get('attempt_id') != turn['attempt_id']:
            return
        turn = database.resolve(sid, mid, turn)
        if turn is None:
            return
        current_trace.get().event('session.resolved_turn', turn, turn_kind=turn['kind'])
        if turn.get('acknowledgement'):
            result = {'status': 'acknowledged', 'answer': turn['acknowledgement'], 'evidence': [],
                      'timings': {'llm_calls': 0, 'retrieval_seconds': 0}}
        elif turn["clarification"]:
            result = {"status": "needs_clarification", "answer": turn["clarification"],
                      "clarification": turn["clarification"], "evidence": [],
                      "timings": {"llm_calls": 0, "retrieval_seconds": 0}}
        elif not index_ready():
            current_trace.get().fail('index_not_ready', 'ingestion.index_gate')
            result = {"status": "index_not_ready", "answer": "知识索引尚未通过构建后验收，本次未生成答案。请待完成后重试。", "evidence": [], "timings": {"llm_calls": 0}}
        else:
            result = run_query(turn["query"], turn["version"], retriever_factory=load_retriever,
                               fact_sources=turn["fact_sources"], analysis_seed=turn.get("analysis_seed"),
                               version_scope=turn.get('version_scope'), resolved_scope=turn.get('resolved_scope'),
                               task=turn_task(turn))
    except FileNotFoundError:
        current_trace.get().fail('index_not_ready', 'ingestion.index_load')
        result = {"status": "index_not_ready", "answer": "知识索引尚未就绪，本次未生成答案。请待构建与验收完成后重试。", "evidence": []}
    except Exception as error:
        current_trace.get().fail(type(error).__name__)
        result = execution_failure(error)
    observed_versions = list(dict.fromkeys(e['version'] for e in current_trace.get().data['events']
        if e['name']=='retrieval.scope_filter' and e.get('version')))
    if observed_versions:
        # 生成失败不抹掉已经执行的检索范围，供下一轮相对版本追问使用。
        result['searched_versions'] = observed_versions
    result.setdefault("timings", {}).update(first_feedback_seconds=round(feedback_seconds, 4),
                                           complete_seconds=round(perf_counter() - started, 4))
    result["turn_kind"] = turn["kind"]
    if turn.get('relation_pending'):
        result['task_relation_pending'] = True
    # 先落盘再把保存状态交付给会话；外层仍会补记提交阶段，但不会另建 Trace。
    current_trace.get().finish(result)
    result['diagnostics'] = current_trace.get().summary()
    with stage('delivery.session_commit'):
        database.finish(sid, mid, result, attempt_id=turn.get('attempt_id'))
    return result


@app.post("/api/sessions/{sid}/turns", status_code=202)
def send_turn(sid: str, body: Turn):
    started = perf_counter()
    database = session_store()
    slots=request_slots
    if not slots.acquire(blocking=False):
        raise HTTPException(503,'当前处理任务已满，请稍后重试；本次未提交。')
    try:
        doc, mid, turn = database.accept(sid, body.query, body.revision)
    except KeyError:
        slots.release()
        raise HTTPException(404, "会话不存在或已删除")
    except SessionBusy as error:
        slots.release()
        raise HTTPException(409, str(error))
    except ValueError as error:
        slots.release()
        raise HTTPException(400, str(error))
    except Exception:
        slots.release()
        raise HTTPException(503,'会话暂时无法保存，本次未提交，请稍后重试。')
    return queue_session_turn(database, sid, doc, mid, turn, started, feedback_seconds=perf_counter()-started,
                              diagnostic=body.diagnostic)


@app.post('/api/sessions/{sid}/messages/{mid}/retry', status_code=202)
def retry_turn(sid: str, mid: str, body: RetryTurn):
    started = perf_counter()
    database = session_store()
    if not request_slots.acquire(blocking=False):
        raise HTTPException(503,'当前服务繁忙，请稍后重试；本次未提交。')
    try:
        doc, mid, turn = database.retry(sid, mid, body.revision)
    except (KeyError,SessionBusy,ValueError) as error:
        request_slots.release()
        raise HTTPException(404 if isinstance(error,KeyError) else 409 if isinstance(error,SessionBusy) else 400,
                            '会话不存在或已删除' if isinstance(error,KeyError) else str(error))
    except Exception:
        request_slots.release()
        raise HTTPException(503,'暂时无法保存重试请求，本次未提交。')
    return queue_session_turn(database, sid, doc, mid, turn, started, perf_counter()-started, body.diagnostic)


def queue_session_turn(database, sid, doc, mid, turn, started, feedback_seconds, diagnostic):
    slots=request_slots
    feedback=feedback_seconds
    # 诊断开关只属于本次调用，不写入会话事实或下一轮材料。
    turn['diagnostic'] = diagnostic
    # 先持久化用户消息和处理中状态；刷新页面仍能恢复，网络断开不会重复发送。
    try:
        future=workers.submit(complete_turn, database, sid, mid, turn, started, feedback)
        future.add_done_callback(lambda _:slots.release())
    except Exception:
        slots.release()
        with request_trace(diagnostic, {'entrypoint': 'http.session', 'session_id': sid, 'message_id': mid}) as trace:
            trace.event('session.turn', turn)
            trace.fail('worker_submission_failed', 'delivery.worker_submit')
            result = {"status": "execution_failed", "answer": "任务未能启动，请重试。", "evidence": []}
            trace.finish(result)
            result['diagnostics'] = trace.summary()
            database.finish(sid, mid, result, attempt_id=turn.get('attempt_id'))
        raise HTTPException(503, "任务未能启动，请重试")
    return {"session": present_session(doc), "message_id": mid, "first_feedback_seconds": round(feedback, 4)}


@app.get("/api/source", response_class=PlainTextResponse)
def source(doc_id: str, start_line: int | None = None, end_line: int | None = None):
    rows = read_jsonl(Path(config()["data_dir"]) / "processed" / "documents.jsonl")
    row = next((row for row in rows if row["doc_id"] == doc_id), None)
    if row is None:
        raise HTTPException(404, "未知文档")
    path = Path(row["original_path"]).resolve()
    if not path.is_relative_to(Path(config()["source_root"]).resolve()):
        raise HTTPException(400, "来源路径超出文档目录")
    if sha256_file(path) != row["source_sha256"]:
        raise HTTPException(409, "源文件已变化，请重新入库后核对")
    text = path.read_text(encoding="utf-8-sig")
    if start_line is not None:
        lines = text.splitlines()
        end_line = end_line if end_line is not None else start_line
        if not 1 <= start_line <= end_line <= len(lines):
            raise HTTPException(400, "原文行号无效")
        # 编号与高亮直接对应经哈希验证的原文，刷新或复制链接仍能定位。
        body = ''.join(f'<div id="L{n}" class="{"hit" if start_line <= n <= end_line else ""}"><a href="#L{n}">{n}</a> {html.escape(line)}</div>'
                       for n, line in enumerate(lines, 1))
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>文档原文</title><style>'
            'body{font:14px/1.7 monospace}div{white-space:pre-wrap;overflow-wrap:anywhere}.hit{background:#fff2bc}'
            'a{color:#788;text-decoration:none;display:inline-block;width:4em}</style>' + body)
    return text
