"""网页、命令行和评测共享的单次理解—检索—证据回答流程。"""
from __future__ import annotations

from .common import config
from .context import enrich_context, contained_evidence
from .generation import answer
from .lexical import exact_literals
from .query_understanding import understand_query
from .telemetry import current_metrics, current_trace, RequestTrace, stage, event, snapshot_rows, fingerprint
from time import perf_counter
import re


def enrich_selection(rows, candidate_rows, query, plan=None):
    """在原候选和预算内回填；每个候选至多尝试一次，不重新召回。"""
    raw = rows
    tried = {r['chunk_id'] for r in rows}
    primary_count = sum(not r.get('reference_root') for r in rows)
    from .retrieval import comparison_objects, object_supported
    subjects = comparison_objects(plan or {})
    refill_docs = set()
    while True:
        enriched = enrich_context(raw, query)
        duplicate = contained_evidence(enriched)
        if not duplicate:
            return enriched
        for chunk_id, covered_by in duplicate.items():
            event('augmentation.redundant_evidence', chunk_id=chunk_id, covered_by=covered_by,
                  reason='same_source_scope_all_content_contained')
        refill_docs.update(r['doc_id'] for r in raw if r['chunk_id'] in duplicate)
        raw = [r for r in raw if r['chunk_id'] not in duplicate]
        for candidate in candidate_rows:
            if sum(not r.get('reference_root') for r in raw) >= primary_count:
                break
            if candidate['chunk_id'] in tried:
                continue
            tried.add(candidate['chunk_id'])
            # 释放名额不等于需要更多主题：仅回填原任务来源，或有原文对象依据的比较候选。
            if candidate['doc_id'] not in refill_docs and not any(object_supported(candidate, obj) for obj in subjects):
                event('augmentation.refill_skipped', chunk_id=candidate['chunk_id'], reason='no_task_scope_support')
                continue
            raw.append({**candidate, 'selection_reason': 'contained_evidence_refill'})
            event('augmentation.refill', chunk_id=candidate['chunk_id'], reason='next_existing_substantive_candidate')
        # 原有显式引用/跨版本补取仍作为补充项，主片段按未修改的RRF分数排列。
        raw.sort(key=lambda r: (bool(r.get('reference_root')), -r.get('score', 0)))
        raw = [{**r, 'rank': n} for n, r in enumerate(raw, 1)]
        snapshot_rows('augmentation.refilled_raw', raw)


def run_query(query: str, version: str | None = None, top_k: int | None = None,
              mode: str | None = None, evidence_only: bool = False, retriever=None,
              retriever_factory=None, fact_sources=None, analysis_seed=None, *, diagnostic=False,
              request_identity=None, version_scope: str | None = None,
              resolved_scope: dict | None = None, task: dict | None = None) -> dict:
    metrics = {"llm_calls": 0, "llm_seconds": 0.0}
    token = current_metrics.set(metrics)
    inherited = current_trace.get()
    trace = inherited or RequestTrace(diagnostic, request_identity)
    trace_token = current_trace.set(trace)
    trace.save()
    started = perf_counter()
    try:
        result = _run_query(query, version, top_k, mode, evidence_only, retriever, metrics, retriever_factory, fact_sources, analysis_seed, version_scope, resolved_scope, task)
        metrics["total_seconds"] = perf_counter() - started
        result["timings"] = {key: round(value, 4) for key, value in metrics.items()}
        event('pipeline.result', result, status=result.get('status'))
        if inherited is None:
            trace.finish(result)
        result['diagnostics'] = trace.summary()
        return result
    except Exception as error:
        trace.fail(type(error).__name__)
        if inherited is None:
            trace.finish({'status': 'execution_failed', 'error_type': type(error).__name__})
        raise
    finally:
        current_trace.reset(trace_token)
        current_metrics.reset(token)


def _run_query(query, version, top_k, mode, evidence_only, retriever, metrics, retriever_factory, fact_sources, analysis_seed, version_scope, resolved_scope, task):
    cfg = config()
    trace = current_trace.get()
    if trace is not None:
        trace.data['identity']['config_sha256'] = fingerprint(cfg)
        event('request.input', {'query': query, 'selected_version': version, 'fact_sources': fact_sources,
                               'analysis_seed': analysis_seed, 'version_scope': version_scope,
                               'resolved_scope':resolved_scope, 'task':task}, query_sha256=fingerprint(query),
              requested_version=version, top_k=top_k or cfg['top_k'],
              retrieval_mode=mode or cfg['retrieval_mode'], evidence_only=evidence_only)
    started = perf_counter()
    with stage('retrieval.understanding'):
        scope_args = {'version_scope': version_scope} if version_scope is not None else {}
        if resolved_scope is not None:
            scope_args['resolved_scope'] = resolved_scope
        if task is not None:
            scope_args['task'] = task
        plan = understand_query(query, version, analysis_seed, **scope_args) if analysis_seed else understand_query(query, version, **scope_args)
    if fact_sources is not None:
        for key in ("facts", "conditions"):
            grounded = []
            for fact in plan.get(key, []):
                for number, source in enumerate(fact_sources):
                    if fact["quote"] in source:
                        start = source.index(fact["quote"])
                        grounded.append({**fact, "source": "session_user_input", "input_index": number,
                                         "start": start, "end": start + len(fact["quote"])})
                        break
            plan[key] = grounded
    metrics["understanding_seconds"] = perf_counter() - started
    event('understanding', plan, status=plan['status'], versions=plan.get('versions', []))
    if plan['status'] in ('understanding_failed', 'understanding_unavailable'):
        trace.fail(plan.get('error_type', plan['status']), 'retrieval.understanding')
    scope = ",".join(plan.get("versions", [])) or version or "auto"
    result = {"understanding": plan, "source_version": scope, "evidence": [], "build_id": None,
              "citations_valid": None}
    if plan["status"] != "ready":
        return {**result, "status": plan["status"], "answer": plan["clarification"]}
    if retriever is None:
        started = perf_counter()
        if retriever_factory is None:
            from .retrieval import Retriever
            retriever_factory = Retriever
        with stage('ingestion.index_load'):
            retriever = retriever_factory()
        metrics["retriever_load_seconds"] = perf_counter() - started
    if trace is not None:
        trace.data['identity']['index_manifest_sha256'] = fingerprint(retriever.manifest)
        trace.data['identity']['index'] = {k: retriever.manifest[k] for k in
            ('build_id', 'embedding_model', 'model_fingerprint', 'dimension', 'token_limit',
             'query_prefix', 'chunks_sha256', 'metadata_sha256', 'index_sha256') if k in retriever.manifest}
    started = perf_counter()
    candidate_rows = []
    with stage('retrieval.search'):
        rows = retriever.search_plan(plan, top_k or cfg["top_k"], mode or cfg["retrieval_mode"], candidate_rows=candidate_rows)
    snapshot_rows('retrieval.output', rows)
    metrics["retrieval_seconds"] = perf_counter() - started
    candidates = rows
    if plan['kind'] == 'knowledge':
        # 完整问题逐字匹配 FAQ 描述时，邻近报错只是候选，不能混成该故障的原因。
        normalize = lambda text: re.sub(r'[\s。？！?!]+', '', text).casefold()
        matching = [r for r in rows if any(normalize(description) == normalize(query)
            for description in re.findall(r'\*{0,2}问题描述\*{0,2}[：:]\s*([^\n]+)', r['chunk_text']))]
        if matching:
            rows = matching
            plan['evidence_selection'] = 'exact_faq_description'
    event('augmentation.selection', {'kept_ids': [r['chunk_id'] for r in rows],
        'decisions': [{'chunk_id': r['chunk_id'], 'selected': r in rows,
                       'reason': 'exact_faq_description' if plan.get('evidence_selection') else 'retrieval_output'}
                      for r in candidates]}, count=len(rows))
    snapshot_rows('augmentation.selected_raw', rows)
    started = perf_counter()
    with stage('augmentation.context_expansion'):
        rows = enrich_selection(rows, candidate_rows, query, plan)
    for row in rows:
        if not any(r['chunk_id'] == row['chunk_id'] for r in candidates):
            candidates.append(row)
    from .retrieval import comparison_objects, object_supported
    for subject in comparison_objects(plan):
        event('augmentation.comparison_coverage', subject=subject,
              supporting_ids=[r['chunk_id'] for r in rows if object_supported(r, subject)])
    snapshot_rows('augmentation.enriched', rows)
    metrics["context_seconds"] = perf_counter() - started
    available_text = "\n".join(r["chunk_text"] + "\n" + "\n".join(c["text"] for c in r["contexts"]) for r in rows)
    plan["unmatched_technical_literals"] = [literal for literal in plan["technical_literals"]
                                           if not exact_literals(available_text, [literal])]
    invalid = [r for r in rows if r["context_status"] in ("source_changed", "source_error", "untrusted_source")]
    if invalid:
        trace.fail('source_integrity_failed', 'augmentation.source_integrity')
        return {**result, "build_id": retriever.manifest["build_id"], "status": "source_integrity_failed",
                "answer": "证据原文校验失败，未生成答案。请核对知识快照。", "evidence": rows}
    result.update(evidence=rows, build_id=retriever.manifest["build_id"],
                  retrieval_coverage={"versions_requested": plan["versions"],
                    "versions_returned": list(dict.fromkeys(r["source_version"] for r in rows)),
                    "subqueries_requested": [q["id"] for q in plan["subqueries"]],
                    "subqueries_returned": sorted({p["subquery_id"] for r in rows for p in r["retrieval_trace"]})})
    plan["retrieval_coverage"] = result["retrieval_coverage"]
    started = perf_counter()
    snapshot_rows('generation.input_rows', rows)
    with stage('generation.answer'):
        generated = ({"status": "evidence_only", "answer": "仅展示证据，未生成答案。", "citations_valid": None}
                     if evidence_only else answer(query, rows, scope, plan))
    if generated['status'] in ('generation_incomplete', 'answer_validation_failed'):
        trace.fail(generated.get('generation_failure_reason', generated['status']), 'generation.output_validation')
    elif generated['status'] == 'llm_not_configured':
        trace.fail('llm_not_configured', 'generation.model_configuration')
    metrics["generation_seconds"] = perf_counter() - started
    # 命中候选与真正被答案采用的依据分开，未引用的片段不冒充答案支持。
    cited = set(generated.get('citation_numbers', []))
    result['retrieval_candidates'] = [{**{k: r.get(k) for k in
        ('rank', 'chunk_id', 'doc_id', 'source_version', 'section_path', 'retrieval_trace')},
        'selected_for_generation': r['chunk_id'] in {e['chunk_id'] for e in rows}} for r in candidates]
    candidate_view=evidence_only or generated['status']=='llm_not_configured'
    result['evidence'] = [{**r, 'evidence_role': 'retrieval_candidate' if candidate_view else 'answer_support'}
                          for r in rows if candidate_view or r['rank'] in cited]
    snapshot_rows('generation.delivered_citations', result['evidence'])
    return {**result, **generated}
