"""正式问答的本地脱敏 Trace；沿用现有旁路观测，不改变问答行为。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .common import ROOT, sha256_file

current_metrics = ContextVar("request_metrics", default=None)
current_trace = ContextVar("request_trace", default=None)
TRACE_DIR = ROOT / 'logs' / 'traces'
_STARTED = datetime.now(timezone.utc).isoformat()
# 在模块加载时固化源码身份，不能把之后磁盘上的新文件冒充已运行的旧实现。
_CODE = {str(p.relative_to(ROOT)).replace('\\', '/'): sha256_file(p)
         for p in sorted((ROOT / 'doris_rag').glob('*.py'))}
_CODE_ID = hashlib.sha256(json.dumps(_CODE, sort_keys=True).encode()).hexdigest()
_CODE_STATS = {name: ((ROOT / name).stat().st_mtime_ns, (ROOT / name).stat().st_size) for name in _CODE}
_write_lock = threading.Lock()


def fingerprint(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=lambda x: type(x).__name__)
    return hashlib.sha256(text.encode()).hexdigest()


def redact(value, secrets=()):
    """不收集认证头；同时遮蔽配置密钥、正文中的凭据赋值及已知服务密钥。"""
    values = [v for k, v in os.environ.items() if re.search(r'KEY|TOKEN|PASSWORD|SECRET', k, re.I) and len(v) >= 6]
    values += [v for v in secrets if isinstance(v, str) and v]
    def clean(item):
        if isinstance(item, dict):
            return {str(k): '[REDACTED]' if re.search(r'(?:^|[_-])(?:authorization|api[_-]?key|password|secret|access[_-]?token|auth[_-]?token|cookie)$', str(k), re.I)
                    else clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, str):
            for secret in values:
                for variant in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]):
                    item = item.replace(variant, '[REDACTED]')
            item = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9_.+/=-]+', r'\1[REDACTED]', item)
            # Cookie 是整行凭据，不能只遮蔽第一个 cookie 值而留下后续会话票据。
            item = re.sub(r'''(?im)((?:set-cookie|cookie|authorization)\s*["']?\s*[:=]\s*)[^\r\n]+''', r'\1[REDACTED]', item)
            item = re.sub(r'''(?ix)((?:api[_-]?key|password|secret|access[_-]?token|auth[_-]?token)\s*["']?\s*[:=]\s*)["']?[^\s,;"'\\}]+["']?''', r'\1[REDACTED]', item)
            return item
        return item if item is None or isinstance(item, (bool, int, float)) else type(item).__name__
    return clean(value)


class RequestTrace:
    def __init__(self, detailed=False, identity=None, *, persist=True):
        self.detailed = detailed
        self.persist = persist
        self.saved = False
        started = datetime.now(timezone.utc)
        trace_id = uuid4().hex
        self.path = TRACE_DIR / (started.strftime('%Y%m%dT%H%M%S%fZ_') + trace_id + '.json')
        self.journal_path = self.path.with_suffix('.jsonl')
        self._previous = {}
        self._event_count = 0
        self._rows = {'stages': [], 'model_calls': []}
        self._revision = 0
        self._snapshot_revision = -1
        self._identity_saved = False
        self._recording_depth = 0
        self.recording_seconds = 0.0
        self.journal_bytes = 0
        self.snapshot_bytes = 0
        self.snapshot_writes = 0
        self.stack = []
        self.prepared = None
        self.secrets = []
        self.data = {'schema_version': 1, 'request_id': trace_id, 'trace_id': trace_id,
            'trace_path': str(self.path.resolve()) if persist else None,
            'detail_mode': detailed, 'started_at': started.isoformat(),
            'identity': {**(identity or {}), 'process_id': os.getpid(), 'trace_module_loaded_at': _STARTED,
                         'code_id': _CODE_ID},
            'stages': [], 'events': [], 'model_calls': [], 'status': 'started',
            'last_completed_stage': None, 'failure': None,
            'timing_semantics': 'stage.seconds=wall_seconds-logging_seconds; end_checkpoint_excluded; nested_inclusive',
            'content_policy': 'redacted_full_content' if detailed else 'metadata_only',
            'retention_hours': None}
        try:
            from .common import config
            cfg = config()
            self.data['identity']['config_sha256'] = fingerprint(cfg)
            self.config = {k: v for k, v in cfg.items() if k in (
                'source_root', 'data_dir', 'embedding_model', 'embedding_model_path', 'embedding_dimension',
                'local_files_only', 'max_embedding_tokens', 'query_prefix', 'torch_threads', 'batch_size',
                'target_body_tokens', 'top_k', 'retrieval_mode', 'default_version', 'expected_full_counts')}
            pointer = Path(cfg['data_dir']) / 'index/current.json'
            self.data['identity']['active_index_pointer'] = json.loads(pointer.read_text(encoding='utf-8'))
        except Exception as error:
            self.data['identity']['index_identity_unavailable'] = type(error).__name__

    @contextmanager
    def recording(self):
        # 外层统一计时，避免event调用save时重复累计脱敏和写入成本。
        outer = self._recording_depth == 0
        started = perf_counter()
        self._recording_depth += 1
        try:
            yield
        finally:
            self._recording_depth -= 1
            if outer:
                self.recording_seconds += perf_counter() - started

    def _write_record(self, value):
        payload = json.dumps({'seq': self._revision + 1, **value}, ensure_ascii=False) + '\n'
        encoded = payload.encode('utf-8')
        # 每条关闭文件，异常退出仍可恢复已提交行；不承诺断电后的fsync持久性。
        with self.journal_path.open('ab') as stream:
            stream.write(encoded)
        self.journal_bytes += len(encoded)
        self._revision += 1

    def _checkpoint(self):
        excluded = {'events', 'stages', 'model_calls', 'recording'}
        changes = {k: v for k, v in self.data.items() if k not in excluded and
                   (k not in self._previous or self._previous[k] != v)}
        record = {'set': changes} if changes else {}
        if self._event_count < len(self.data['events']):
            record['events'] = self.data['events'][self._event_count:]
        updated = {}
        for field, previous in self._rows.items():
            entries = []
            for i, row in enumerate(self.data[field]):
                old = previous[i] if i < len(previous) else {}
                delta = {k: v for k, v in row.items() if k not in old or old[k] != v}
                if delta:
                    entries.append({'index': i, 'set': delta})
            if entries:
                record[field] = entries
                updated[field] = copy.deepcopy(self.data[field])
        if not record:
            return
        # 只处理新增数据，不再重复序列化此前的候选、正文及模型响应。
        self._write_record(redact(record, self.secrets))
        self._previous.update(copy.deepcopy(changes))
        self._rows.update(updated)
        self._event_count = len(self.data['events'])

    def save(self, *, final=False):
        if not self.persist:
            return
        try:
            with self.recording():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if not self._identity_saved:
                    with _write_lock:
                        identities = self.path.parent / 'identities'
                        identities.mkdir(exist_ok=True)
                        manifest = identities / ('code-' + _CODE_ID + '.json')
                        if not manifest.exists():
                            manifest.write_text(json.dumps({'code_id': _CODE_ID, 'files': _CODE}, indent=2), encoding='utf-8')
                        cfg = self.data['identity'].get('config_sha256')
                        if cfg and hasattr(self, 'config'):
                            manifest = identities / ('config-' + cfg + '.json')
                            if not manifest.exists():
                                manifest.write_text(json.dumps(redact(self.config, self.secrets), ensure_ascii=False, indent=2), encoding='utf-8')
                    self._identity_saved = True
                self._checkpoint()
                if final and self._snapshot_revision != self._revision:
                    self.data['recording'] = {
                        'seconds_before_snapshot': round(self.recording_seconds, 6),
                        'journal_records': self._revision, 'journal_bytes': self.journal_bytes,
                        'prior_snapshot_bytes': self.snapshot_bytes, 'prior_snapshot_writes': self.snapshot_writes,
                        'recovery_path': str(self.journal_path.resolve())}
                    encoded = json.dumps(redact(self.data, self.secrets), ensure_ascii=False, indent=2).encode('utf-8')
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_bytes(encoded)
                    temporary.replace(self.path)
                    self.snapshot_writes += 1
                    self.snapshot_bytes += len(encoded)
                    self._snapshot_revision = self._revision
                self.saved = True
        except Exception as error:
            # 诊断写入失败不能改变排序、触发重试或覆盖业务异常。
            self.data['recording_error'] = type(error).__name__

    def event(self, name, value=None, **summary):
        try:
            with self.recording():
                event = {'name': name, 'stage': self.stack[-1] if self.stack else None, **summary}
                if self.detailed:
                    event['data'] = value
                self.data['events'].append(redact(copy.deepcopy(event), self.secrets))
                self.save()
        except Exception as error:
            self.data['recording_error'] = type(error).__name__

    def fail(self, error_type, stage=None):
        if self.data['failure'] is None:
            self.data['failure'] = {'stage': stage or (self.stack[-1] if self.stack else 'request'),
                'error_type': error_type, 'last_completed_stage': self.data['last_completed_stage']}

    def finish(self, result):
        with self.recording():
            self._finish(result)

    def _finish(self, result):
        self.data.update(status=result.get('status', 'failed'), completed_at=datetime.now(timezone.utc).isoformat())
        self.data['result'] = redact(copy.deepcopy(result), self.secrets) if self.detailed else {
            'status': result.get('status'), 'answer_sha256': fingerprint(result.get('answer', '')),
            'citation_numbers': result.get('citation_numbers', []),
            'evidence_ids': [r.get('chunk_id') for r in result.get('evidence', [])]}
        if self.data['failure']:
            self.data['last_completed_stage'] = self.data['failure']['last_completed_stage']
        try:
            self.data['identity']['source_drift_since_trace_import'] = [name for name, stamp in _CODE_STATS.items()
                if ((ROOT / name).stat().st_mtime_ns, (ROOT / name).stat().st_size) != stamp]
        except OSError as error:
            self.data['identity']['source_drift_check_error'] = type(error).__name__
        self.save(final=True)

    def summary(self):
        value = {k: self.data.get(k) for k in ('request_id', 'detail_mode', 'last_completed_stage', 'failure', 'recording_error')}
        value.update(trace_id=self.data['trace_id'], trace_path=str(self.path.resolve()) if self.saved else None,
                     recovery_path=str(self.journal_path.resolve()) if self.saved else None,
                     recording_seconds=round(self.recording_seconds, 6),
                     save_status='disabled' if not self.persist else 'failed' if value['recording_error'] else 'saved' if self.saved else 'pending',
                     session_id=self.data['identity'].get('session_id'), message_id=self.data['identity'].get('message_id'))
        if value['failure']:
            value['last_completed_stage'] = value['failure']['last_completed_stage']
        return redact(value, self.secrets)


@contextmanager
def stage(name):
    trace = current_trace.get()
    if trace is None:
        yield
        return
    full = '/'.join([*trace.stack, name])
    item = {'name': full, 'status': 'started'}
    trace.data['stages'].append(item)
    trace.stack.append(name)
    started, logging_start = perf_counter(), trace.recording_seconds
    trace.save()
    try:
        yield
    except Exception as error:
        item.update(status='failed', error_type=type(error).__name__)
        trace.fail(type(error).__name__, full)
        raise
    else:
        item['status'] = 'completed'
        trace.data['last_completed_stage'] = full
    finally:
        wall = perf_counter() - started
        logging = trace.recording_seconds - logging_start
        # seconds为执行区间扣除同步观测后的耗时；父阶段仍包含子阶段执行。
        item.update(seconds=round(max(0, wall - logging), 6), wall_seconds=round(wall, 6),
                    logging_seconds=round(logging, 6))
        trace.stack.pop()
        trace.save()


def traced(name):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return run
    return decorate


@contextmanager
def request_trace(detailed=False, identity=None, *, persist=True):
    trace = RequestTrace(detailed, identity, persist=persist)
    token = current_trace.set(trace)
    trace.save()
    try:
        yield trace
    except Exception as error:
        trace.fail(type(error).__name__)
        trace.finish({'status': 'execution_failed', 'error_type': type(error).__name__})
        raise
    finally:
        trace.save(final=True)
        current_trace.reset(token)


def load_trace(path):
    """兼容旧JSON；未完成的请求从追加记录恢复，尾部半行不冒充已提交记录。"""
    path = Path(path)
    snapshot = None
    if path.suffix != '.jsonl' and path.exists():
        snapshot = json.loads(path.read_text(encoding='utf-8'))
    journal = path if path.suffix == '.jsonl' else path.with_suffix('.jsonl')
    if not journal.exists() and snapshot is not None:
        return snapshot
    data = {'events': [], 'stages': [], 'model_calls': []}
    lines = journal.read_bytes().splitlines(keepends=True)
    tail = False
    sequence = 0
    for i, line in enumerate(lines):
        if not line.endswith(b'\n') and i == len(lines) - 1:
            tail = True
            break
        value = json.loads(line)
        if value['seq'] != i + 1:
            raise ValueError('Trace追加记录序号不连续')
        sequence = value['seq']
        data.update(value.get('set', {}))
        data['events'].extend(value.get('events', []))
        for field in ('stages', 'model_calls'):
            for entry in value.get(field, []):
                index = entry['index']
                if index == len(data[field]):
                    data[field].append({})
                data[field][index].update(entry['set'])
    # 会话提交可能发生在第一次finish之后，不能让旧快照遮住后续故障。
    stale = snapshot is not None and snapshot.get('recording', {}).get('journal_records') != sequence
    if snapshot is not None and not stale and not tail:
        return snapshot
    data['recovery'] = {'source': str(journal), 'incomplete_tail': tail, 'snapshot_stale': stale}
    return data


def event(name, value=None, **summary):
    trace = current_trace.get()
    if trace is not None:
        trace.event(name, value, **summary)


def rows_metadata(rows, content=False):
    keys = ('chunk_id', 'doc_id', 'rank', 'source_version', 'source_sha256', 'relative_path',
            'source_start_line', 'source_end_line', 'score', 'selection_reason', 'context_status', 'context_issues')
    return [{**{k: row[k] for k in keys if k in row},
             **({'chunk_text': row.get('chunk_text', '')} if content else {})} for row in rows]


def snapshot_rows(name, rows):
    trace = current_trace.get()
    if trace is not None:
        trace.event(name, rows if trace.detailed else None, count=len(rows), rows=rows_metadata(rows))


def observe_exchange(original, url, args, kwargs, call, save, redactor=lambda x: x, detailed=True):
    """专项 Trace 与正常调用共用同一旁路，不改变参数、响应对象或异常。"""
    trace = current_trace.get()
    recording = trace.recording if trace is not None else nullcontext
    with recording():
        payload = kwargs.get('json')
        call.update(status='started', request_sha256=fingerprint(payload))
        if detailed:
            call['request'] = redactor(copy.deepcopy(payload))
            call['request_redacted'] = fingerprint(call['request']) != call['request_sha256']
        save()
    started = perf_counter()
    elapsed = None
    try:
        response = original(url, *args, **kwargs)
        elapsed = perf_counter() - started
        with recording():
            call.update(status='received', http_status=response.status_code)
            if detailed:
                call['raw_response'] = redactor(response.text)
                call['response_redacted'] = call['raw_response'] != response.text
        return response
    except Exception as error:
        elapsed = perf_counter() - started if elapsed is None else elapsed
        with recording():
            call.update(status='request_failed', error_type=type(error).__name__)
        raise
    finally:
        with recording():
            call['seconds'] = round(perf_counter() - started if elapsed is None else elapsed, 6)
            call['timing_semantics'] = 'http_call_only'
            save()


def model_post(original, url, *, secret='', **kwargs):
    trace = current_trace.get()
    if trace is None:
        return original(url, **kwargs)
    with trace.recording():
        payload = kwargs.get('json') or {}
        if secret:
            trace.secrets.append(secret)
        call = {k: payload[k] for k in ('model', 'max_tokens', 'temperature', 'response_format') if k in payload}
        call['stage'] = '/'.join(trace.stack)
        try:
            from urllib.parse import urlsplit
            call['server_address'] = urlsplit(url).hostname
        except ValueError:
            call['server_address'] = None
        call['messages_sha256'] = fingerprint(payload.get('messages'))
        if trace.prepared and trace.prepared['messages_sha256'] == call['messages_sha256']:
            call['evidence_units'] = trace.prepared['evidence_units']
        trace.data['model_calls'].append(call)
    with stage('model.transport'):
        return observe_exchange(original, url, (), kwargs, call, trace.save,
                                lambda value: redact(value, [secret]), trace.detailed)


def evidence_path(trace, chunk_id):
    """只重建结构去向；同源跨度覆盖不等于已证明陈述受支持。"""
    if not trace.get('detail_mode'):
        return {'status': 'insufficient_trace', 'reason': 'raw_candidates_not_retained_in_light_mode'}
    events = trace.get('events', [])
    def rows(name):
        return [r for e in events if e['name'] == name for r in (e.get('data') or [])]
    raw = [r for name in ('retrieval.raw_dense', 'retrieval.raw_bm25', 'retrieval.raw_exact')
           for r in rows(name) if r.get('chunk_id') == chunk_id]
    if not raw:
        return {'status': 'not_observed', 'reason': 'not_in_recorded_raw_candidates',
                'remaining_scope': ['scope_filter', 'channel_recall', 'earlier_unobserved_stages']}
    path, first = [], None
    stages = [('retrieval.channel_rrf', None), ('retrieval.group_pool', 'channel_fusion_pool_limit'),
              ('retrieval.subquery_version_rrf', None), ('retrieval.substantive_filter', 'no_substantive_body'),
              ('retrieval.primary_selection', 'selection_capacity_and_coverage_policy'),
              ('retrieval.selected_with_extensions', 'not_in_retrieval_output'),
              ('augmentation.selected_raw', 'exact_faq_description'), ('generation.input_rows', 'not_in_generation_rows')]
    for name, reason in stages:
        recorded = any(e['name'] == name for e in events)
        present = any(r.get('chunk_id') == chunk_id for r in rows(name)) if recorded else None
        path.append({'stage': name, 'present_as_primary': present})
        if present is False and first is None:
            first = {'stage': name, 'reason': reason or 'not_in_recorded_output'}
    calls = [c for c in trace.get('model_calls', []) if 'evidence_units' in c]
    if not calls:
        return {'status': 'partial_trace', 'path': path, 'first_primary_exclusion': first,
                'present_in_model_input': None, 'reason': 'no_observed_generation_request'}
    units = calls[-1]['evidence_units']
    target = raw[0]
    primary = any(u.get('chunk_id') == chunk_id and u.get('snippet_included') for u in units)
    context = any(u['doc_id'] == target.get('doc_id') and c.get('source_sha256') == target.get('source_sha256')
        and c.get('source_sha256') is not None
        and c.get('source_start_line', 1) <= target.get('source_start_line', 0)
        and c.get('source_end_line', 0) >= target.get('source_end_line', 1)
        for u in units for c in u.get('contexts', []))
    return {'status': 'observed', 'chunk_id': chunk_id, 'path': path, 'first_primary_exclusion': first,
            'present_in_model_input': primary or context,
            'model_input_role': 'primary' if primary else 'context' if context else 'absent',
            'first_missing_before_model': first if not (primary or context) else None,
            'scope': 'structural_lineage_not_semantic_entailment'}


def llm_started():
    value = current_metrics.get()
    if value is not None:
        value["llm_calls"] += 1
    return perf_counter()


def llm_finished(start):
    value = current_metrics.get()
    if value is not None:
        value["llm_seconds"] += perf_counter() - start
