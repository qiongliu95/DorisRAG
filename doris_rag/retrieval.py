from __future__ import annotations

import json
import re
import threading
from urllib.parse import unquote,urlsplit
from contextlib import nullcontext
from pathlib import Path

import faiss
import numpy as np

from .common import config, model, model_fingerprint, sha256_file, tokens
from .lexical import lexical_tokens
from .query_understanding import FAMILIES, technical_literals, version_mentions
from .telemetry import current_trace, stage, event, snapshot_rows, rows_metadata, fingerprint


def trace_ranking(name, rows, ranking, **scope):
    trace = current_trace.get()
    if trace is not None:
        values = None
        if trace.detailed:
            values = [{**rows_metadata([rows[i]], content=True)[0], 'index': i, 'rank': n, 'score': float(score)}
                      for n, (i, score) in enumerate(ranking, 1)]
        trace.event(name, values, count=len(ranking), **scope)


def comparison_versions(query: str) -> list[str]:
    """只提取比较时的来源目录族，不推断任何功能的适用范围。"""
    return list(dict.fromkeys(m["family"] for m in version_mentions(query) if m["supported_source"]))


def identifier_matches(query: str, row: dict) -> int:
    from .lexical import exact_literals
    text = row["title"] + "\n" + row["section_path"] + "\n" + row["chunk_text"]
    return len(exact_literals(text, technical_literals(query)))


def rrf(rankings: list[list[tuple[int, float]]]) -> list[tuple[int, float]]:
    fused = {}
    for ranking in rankings:
        for rank, (i, _) in enumerate(ranking, 1):
            fused[i] = fused.get(i, 0.0) + 1 / (60 + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


def substantive(text: str) -> bool:
    """标题、提示容器和图片占位仍可帮助导航，但不能单独支持答案。"""
    body = re.sub(r"(?m)^\s{0,3}#{1,6}\s+.*$|^\s*:::[^\n]*$|^\s*`{3,}[^\n]*$|^\s*~{3,}[^\n]*$", "", text)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>", "", body)
    return bool(re.search(r"[\w\u4e00-\u9fff]", body))


def comparison_objects(plan: dict) -> list[str]:
    """消费理解阶段已确定的对象；不从材料或模型概括重新猜测。"""
    return list(plan.get('comparison_objects', [])) if plan.get('kind') == 'knowledge' else []


def explicit_named_terms(plan: dict) -> list[str]:
    """仅覆盖用户逐字给出的多词技术名称，不从日志推导新的诊断对象。"""
    if plan.get('kind') != 'knowledge':
        return []
    terms = list(dict.fromkeys(re.findall(r'\b[A-Z][A-Za-z0-9]*(?:[ \t]+[A-Z][A-Za-z0-9]*){1,5}\b',
                                         plan.get('raw_query', ''))))
    return [t for t in terms if not any(t != other and t in other for other in terms)]


def object_supported(row: dict, subject: str) -> bool:
    from .lexical import exact_literals
    body = re.sub(r'(?m)^\s{0,3}#{1,6}\s+.*$', '', row['chunk_text'] + '\n' +
                  '\n'.join(c['text'] for c in row.get('contexts', [])))
    # 标题提供对象作用域，但独立证据仍须有实际正文；标题命中本身不算覆盖。
    return substantive(body) and bool(exact_literals(
        row.get('title', '') + '\n' + row.get('section_path', '') + '\n' + body, [subject]))


class Retriever:
    def __init__(self):
        self.cfg = config()
        data = Path(self.cfg["data_dir"])
        pointer = json.loads((data / "index" / "current.json").read_text(encoding="utf-8"))
        directory = data / "builds" / pointer["build_id"]
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if sha256_file(data / "processed" / "chunks.jsonl") != self.manifest["chunks_sha256"]:
            raise ValueError("当前数据与活动索引不一致，请重新构建索引")
        for name, key in (("faiss.index", "index_sha256"), ("chunk_metadata.json", "metadata_sha256")):
            if sha256_file(directory / name) != self.manifest[key]:
                raise ValueError(f"索引产物校验失败：{name}")
        if (self.cfg["embedding_model"] != self.manifest["embedding_model"]
                or self.cfg["query_prefix"] != self.manifest["query_prefix"]):
            raise ValueError("当前模型/查询配置与已构建索引不一致")
        if self.cfg["max_embedding_tokens"] != self.manifest["token_limit"]:
            raise ValueError("切分预算与索引不一致，请重新处理数据并构建")
        self.rows = json.loads((directory / "chunk_metadata.json").read_text(encoding="utf-8"))
        self.index = faiss.read_index(str(directory / "faiss.index"))
        if self.index.ntotal != len(self.rows) or self.index.d != self.manifest["dimension"]:
            raise ValueError("向量与 metadata 数量或维度不一致")
        self.encoder = model()
        if model_fingerprint(self.encoder) != self.manifest["model_fingerprint"]:
            raise ValueError("查询模型权重/tokenizer 与构建索引时不同")
        self.version_indexes = {}
        self.lexical = None
        self.search_lock = threading.Lock()

    def candidates(self, version: str):
        if version == "all":
            return self.index, list(range(len(self.rows)))
        if version not in ("4.x", "3.x", "2.1"):
            raise ValueError("不支持的来源版本")
        if version not in self.version_indexes:
            ids = [i for i, row in enumerate(self.rows) if row["source_version"] == version]
            subset = faiss.IndexFlatIP(self.index.d)
            if ids:
                subset.add(np.asarray([self.index.reconstruct(i) for i in ids], dtype=np.float32))
            self.version_indexes[version] = subset, ids
        return self.version_indexes[version]

    def query_vector(self, query):
        return self.query_vectors([query])

    def query_vectors(self, queries):
        texts = [(self.manifest['query_prefix'] + q.strip()).lower() for q in queries]
        event('retrieval.embedding_input', {'texts': texts}, count=len(texts), token_limit=self.encoder.max_seq_length)
        if any(tokens(self.encoder.tokenizer, text)>self.encoder.max_seq_length for text in texts):
            raise ValueError("知识子问题超过 embedding 上限；请缩短目标，不可静默截断")
        # 相同模型批量编码必要的子问题，仍逐条校验预算且跨版本复用；不减少检索目标。
        return self.encoder.encode(texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=len(texts))

    def _rank(self, query: str, version: str, pool: int, mode: str, literals: list[str], vector=None) -> list[dict]:
        index, mapping = self.candidates(version)
        scope = {'query_sha256': fingerprint(query), 'version': version, 'pool': pool}
        event('retrieval.scope_filter', {'predicate': {'source_version': version}},
              total_count=len(self.rows), eligible_count=len(mapping), **scope)
        channels = {}
        if mode != "lexical":
            with stage('dense'):
                vector = self.query_vector(query) if vector is None else vector
                scores, ids = index.search(np.asarray(vector, dtype=np.float32), min(pool, index.ntotal))
            channels["dense"] = [(mapping[int(i)], float(score)) for score, i in zip(scores[0], ids[0]) if i >= 0]
            trace_ranking('retrieval.raw_dense', self.rows, channels['dense'], **scope)
        if mode != "dense":
            with stage('bm25'):
                if self.lexical is None:
                    from .lexical import LexicalIndex
                    self.lexical = LexicalIndex(self.rows)
                channels["bm25"] = self.lexical.ranked(query, mapping, pool)
            trace_ranking('retrieval.raw_bm25', self.rows, channels['bm25'], **scope)
            with stage('exact'):
                channels["exact"] = self.lexical.exact_ranked(literals, mapping, pool)
            trace_ranking('retrieval.raw_exact', self.rows, channels['exact'], **scope)
        with stage('channel_fusion'):
            ranking = rrf(list(channels.values()))
        trace_ranking('retrieval.channel_rrf', self.rows, ranking, **scope)
        trace_ranking('retrieval.group_pool', self.rows, ranking[:pool], cutoff_reason='channel_fusion_pool_limit', **scope)
        result = []
        for rank, (i, score) in enumerate(ranking[:pool], 1):
            hits = []
            for channel, values in channels.items():
                for channel_rank, (candidate, raw_score) in enumerate(values, 1):
                    if candidate == i:
                        hits.append({"channel": channel, "rank": channel_rank, "raw_score": raw_score,
                                     "rrf_contribution": 1 / (60 + channel_rank)})
                        break
            result.append({"index": i, "rank": rank, "score": score, "channel_hits": hits})
        return result

    def search_plan(self, plan: dict, top_k: int = 5, mode: str = "hybrid_precise", *, candidate_rows=None) -> list[dict]:
        # 仅串行化共享 CPU 编码器及惰性索引初始化，LLM 网络等待不持有此锁。
        with getattr(self, "search_lock", nullcontext()):
            result = self._search_plan(plan, top_k, mode, candidate_rows)
            snapshot_rows('retrieval.selected_with_extensions', result)
            return result

    def _search_plan(self, plan: dict, top_k: int, mode: str, candidate_rows=None) -> list[dict]:
        if plan.get("status") != "ready":
            raise ValueError("查询理解未就绪，拒绝执行检索")
        if not 1 <= top_k <= 30 or mode not in ("dense", "lexical", "hybrid", "hybrid_precise"):
            raise ValueError("检索模式或 top_k 不正确")
        subqueries, versions = plan["subqueries"], plan["versions"]
        if not 1 <= len(subqueries) <= 4 or not versions or any(v not in FAMILIES for v in versions):
            raise ValueError("子问题或来源范围超出约束")
        groups, provenance = [], {}
        pool = max(50, top_k * 10)
        with stage('query_encoding'):
            vectors = self.query_vectors([q['text'] for q in subqueries]) if mode != 'lexical' else None
        event('retrieval.query_encoding', {'subqueries': subqueries, 'query_prefix': self.manifest.get('query_prefix')},
              count=len(subqueries), vector_shape=list(vectors.shape) if vectors is not None else None)
        for qnumber, subquery in enumerate(subqueries):
            # 同一子问题的查询向量跨来源范围复用，只改变候选集合，不重复 CPU 编码。
            vector = vectors[qnumber:qnumber+1] if vectors is not None else None
            for version in versions:
                with stage('rank_group'):
                    event('retrieval.group_start', subquery, subquery_id=subquery['id'], version=version)
                    ranked = self._rank(subquery["text"], version, pool, mode, subquery["technical_literals"], vector)
                groups.append([(r["index"], r["score"]) for r in ranked])
                for row in ranked:
                    provenance.setdefault(row["index"], []).append({"subquery_id": subquery["id"],
                        "query": subquery["text"], "source_version": version, "group_rank": row["rank"],
                        "group_rrf_score": row["score"], "merge_rrf_contribution": 1 / (60 + row["rank"]),
                        "channels": row["channel_hits"]})
        # 保留原始通道排名及导航候选；在名额分配前排除没有事实正文的独立引用。
        with stage('subquery_version_fusion'):
            fused = rrf(groups)
        trace_ranking('retrieval.subquery_version_rrf', self.rows, fused)
        with stage('substantive_filter'):
            ranking = [(i, score) for i, score in fused if substantive(self.rows[i]['chunk_text'])]
        trace_ranking('retrieval.substantive_filter', self.rows, ranking, exclusion_reason='no_substantive_body')
        eligible = {i for i, _ in ranking}
        # 版本/子问题覆盖是候选选择约束；选中证据仍按 RRF 排序，不按精确词数量硬置顶。
        selected = set()
        for version in versions:
            candidate = next((i for i, _ in ranking if self.rows[i]["source_version"] == version), None)
            if candidate is not None and len(selected) < top_k:
                selected.add(candidate)
                event('retrieval.selection_step', reason='version_coverage', chunk_id=self.rows[candidate]['chunk_id'])
        # 基础对象的实现入口不能被场景页的同名术语替代；只使用已有对象词及已召回的 Tab 资料。
        foundation_scopes = []
        for q in subqueries:
            subject, separator, _ = q['text'].partition(' 的')
            focus = set(lexical_tokens(subject.removeprefix('Doris ')))
            if q.get('purpose') != 'foundation' or not separator or len(focus) < 2:
                continue
            for version in versions:
                matching = [i for i in eligible if self.rows[i]['source_version'] == version
                    and self.rows[i].get('tab_labels') and self.rows[i].get('scope_ids')
                    and focus <= set(lexical_tokens(self.rows[i]['chunk_text']))
                    and any(p['subquery_id'] == q['id'] for p in provenance[i])]
                if not matching:
                    continue
                candidate = min(matching, key=lambda i: (min(p['group_rank'] for p in provenance[i]
                    if p['subquery_id'] == q['id']), next(n for n, (j, _) in enumerate(ranking) if j == i)))
                represented = any(self.rows[i]['doc_id'] == self.rows[candidate]['doc_id'] for i in selected)
                if not represented and len(selected) < top_k:
                    selected.add(candidate)
                    event('retrieval.selection_step', reason='foundation_scope_coverage',
                          subquery_id=q['id'], chunk_id=self.rows[candidate]['chunk_id'])
                covered = represented or candidate in selected
                event('retrieval.foundation_scope', {'focus_tokens': sorted(focus)},
                      subquery_id=q['id'], chunk_id=self.rows[candidate]['chunk_id'],
                      selected_or_anchored=covered, reason='existing_document' if represented else
                      'foundation_scope_coverage' if covered else 'selection_capacity')
                if covered:
                    foundation_scopes.append((candidate, q['id']))
        for subject in comparison_objects(plan):
            matching = [i for i, _ in ranking if object_supported(self.rows[i], subject)]
            covered = any(i in selected for i in matching)
            if not covered and matching and len(selected) < top_k:
                selected.add(matching[0])
                covered = True
                event('retrieval.selection_step', reason='comparison_object_coverage',
                      subject=subject, chunk_id=self.rows[matching[0]]['chunk_id'])
            event('retrieval.comparison_coverage', subject=subject, covered=covered,
                  candidate_ids=[self.rows[i]['chunk_id'] for i in matching],
                  reason='selected' if covered else 'selection_capacity' if matching else 'no_matching_candidate')
        # 标识符覆盖不能代替显式多词名称覆盖；只在已有有效候选内留位，版本之间不互相替代。
        for term in explicit_named_terms(plan):
            for version in versions:
                matching = [i for i, _ in ranking if self.rows[i]['source_version']==version
                            and term.casefold() in self.rows[i]['chunk_text'].casefold()]
                covered = any(i in selected for i in matching)
                if not covered and matching and len(selected)<top_k:
                    selected.add(matching[0])
                    covered = True
                    event('retrieval.selection_step', reason='explicit_named_term_coverage',
                          term=term, version=version, chunk_id=self.rows[matching[0]]['chunk_id'])
                event('retrieval.named_term_coverage', term=term, version=version, covered=covered,
                      candidate_ids=[self.rows[i]['chunk_id'] for i in matching],
                      reason='selected' if covered else 'selection_capacity' if matching else 'not_recalled')
        if len(subqueries)>1:
            # 同一来源跨多个子问题命中了不同切片时，保留最强文档共识；长文档每组只计一次最佳名次。
            for version in versions:
                scores,targets={},{}
                for group in groups:
                    seen_docs=set()
                    for position,(i,_) in enumerate(group,1):
                        if i not in eligible:continue
                        row=self.rows[i];doc=row['doc_id']
                        if row['source_version']!=version or doc in seen_docs:continue
                        seen_docs.add(doc);scores[doc]=scores.get(doc,0)+1/(60+position)
                        targets[doc]=targets.get(doc,0)+1
                represented={self.rows[i]['doc_id'] for i in selected}
                consensus=sorted((doc for doc in scores if targets[doc]>1 and doc not in represented),key=lambda doc:(-scores[doc],doc))
                if consensus and len(selected)<top_k:
                    selected.add(next(i for i,_ in ranking if self.rows[i]['doc_id']==consensus[0]))
                    event('retrieval.selection_step', reason='document_consensus', doc_id=consensus[0])
        for number, subquery in enumerate(subqueries):
            # 进入某子问题的50条候选不等于覆盖该目标；重复的首条证据也不能挤掉另一目标的次优依据。
            ordered = sorted((i for i in eligible if any(p['subquery_id'] == subquery['id'] for p in provenance[i])),
                             key=lambda i: (min(p['group_rank'] for p in provenance[i] if p['subquery_id'] == subquery['id']),
                                            next(n for n, (j, _) in enumerate(ranking) if j == i)))
            if number == 0 and ordered and ordered[0] in selected:
                continue
            candidate = next((i for i in ordered if i not in selected), None)
            if candidate is not None and len(selected) < top_k:
                selected.add(candidate)
                event('retrieval.selection_step', reason='subquery_coverage', subquery_id=subquery['id'], chunk_id=self.rows[candidate]['chunk_id'])
        for i, _ in ranking:
            if len(selected) >= top_k:
                break
            selected.add(i)
            event('retrieval.selection_step', reason='rrf_fill', chunk_id=self.rows[i]['chunk_id'])
        trace_ranking('retrieval.primary_selection', self.rows, [(i, s) for i, s in ranking if i in selected],
                      exclusion_reason='selection_capacity_and_coverage_policy', top_k=top_k)
        output = []
        seen_ids = set()
        for position, (i, score) in enumerate(ranking, 1):
            if (i not in selected and candidate_rows is None) or self.rows[i]["chunk_id"] in seen_ids:
                if i in selected:
                    event('retrieval.duplicate_chunk_id', chunk_id=self.rows[i]['chunk_id'], index=i)
                continue
            seen_ids.add(self.rows[i]["chunk_id"])
            # 同一来源的独立子问题小节作为关联原文；不再只支持Tab，也不另做搜索。
            anchors = []
            for q in subqueries:
                candidates = [(min(p['group_rank'] for p in provenance[j] if p['subquery_id']==q['id']),j)
                              for j in provenance if self.rows[j]['doc_id']==self.rows[i]['doc_id']
                              and any(p['subquery_id']==q['id'] for p in provenance[j])]
                if candidates:
                    _, j = min(candidates)
                    choices={j}
                    # 概览行可能比具体客户端页排名高；保留同源最佳Tab候选，不能让概览遮掉完整操作。
                    scoped=[entry for entry in candidates if self.rows[entry[1]].get('tab_labels')]
                    if scoped:choices.add(min(scoped)[1])
                    for j in sorted(choices):
                        if j==i:continue
                        anchors.append({'chunk_id':self.rows[j]['chunk_id'], 'scope_ids':self.rows[j].get('scope_ids', []),
                                        'source_span':self.rows[j].get('related_context') or self.rows[j].get('section_context'),
                                        'subquery_id':q['id']})
            for j, qid in foundation_scopes:
                if self.rows[j]['doc_id'] == self.rows[i]['doc_id']:
                    anchors.append({'chunk_id': self.rows[j]['chunk_id'], 'scope_ids': self.rows[j]['scope_ids'],
                                    'subquery_id': qid, 'role': 'foundation_scope'})
            # 召回到的同功能限制不只是导航：保留其已解析、闭合的原作用域，不按整份文档泛化。
            seen_conditions = set()
            families = {tuple(path.split(' > ')[:2]) for path in self.rows[i].get('section_paths', [])
                        if len(path.split(' > ')) >= 2}
            for j, _ in ranking:
                candidate = self.rows[j]
                if candidate['doc_id'] != self.rows[i]['doc_id'] or candidate.get('tab_labels', []) != self.rows[i].get('tab_labels', []):
                    continue
                if not any(tuple(path.split(' > ')[:2]) in families for path in candidate.get('section_paths', [])):
                    continue
                for condition in candidate.get('applicability_conditions', []):
                    sid = condition['scope_id']
                    if condition.get('kind') != 'admonition' or not condition.get('closed') or sid not in candidate.get('scope_ids', []) or sid in seen_conditions:
                        continue
                    seen_conditions.add(sid)
                    qid = min(provenance[j], key=lambda p: p['group_rank'])['subquery_id']
                    anchors.append({'chunk_id': candidate['chunk_id'], 'scope_ids': [sid],
                                    'subquery_id': qid, 'role': 'recalled_constraint'})
                    event('retrieval.context_anchor', chunk_id=self.rows[i]['chunk_id'],
                          from_candidate=candidate['chunk_id'], scope_id=sid, reason='recalled_constraint')
            evidence = {**self.rows[i], "rank": len(output) + 1 if i in selected else position, "score": score,
                           "context_anchors": anchors,
                           "retrieval_mode": mode, "build_id": self.manifest["build_id"],
                           "retrieval_trace": provenance[i], "fusion": "rrf_k60_channels_then_subqueries",
                           "scope_policy": "version_foundation_scope_and_subquery_coverage_then_rrf"}
            if i in selected:
                output.append(evidence)
            if candidate_rows is not None:
                candidate_rows.append(evidence)
        if len(versions)>1:
            # 比较时补齐已选页面在另一请求版本中的候选，不能用单版正文代替版本条件。
            for evidence in output:
                for version in versions:
                    if any(r['relative_path']==evidence['relative_path'] and r['source_version']==version for r in output):continue
                    j=next((i for i,_ in ranking if self.rows[i]['relative_path']==evidence['relative_path']
                            and self.rows[i]['source_version']==version),None)
                    if j is None:continue
                    output.append({**self.rows[j],'rank':len(output)+1,'score':dict(ranking)[j],
                        'retrieval_mode':mode,'build_id':self.manifest['build_id'],'retrieval_trace':provenance[j],
                        'fusion':'rrf_candidate_version_counterpart','selection_reason':'version_counterpart',
                        'context_reference':{'from_chunk_id':evidence['chunk_id'],'label':evidence['relative_path'],
                                             'target':self.rows[j]['relative_path']},'reference_root':True})
                    return output  # 与显式引用共用最多一个补取名额，不扩展查询范围或递归补取。
        # 基础证据明确引用的文档可补取一跳；仅从同版本的现有候选取回，不扩范围或递归抓取。
        basics={q['id'] for q in subqueries if q.get('purpose')=='foundation'}
        if basics:
            by_path={str(Path(self.rows[i]['original_path']).resolve()):i for i in eligible if self.rows[i].get('original_path')}
            for evidence in sorted(output,key=lambda row:min((p['group_rank'] for p in row['retrieval_trace'] if p['subquery_id'] in basics),default=999)):
                if not any(p['subquery_id'] in basics for p in evidence['retrieval_trace']):continue
                text=evidence['chunk_text']
                for match in re.finditer(r'\[([^\]\n]+)\]\(([^\s)]+)\)',text):
                    if not re.search(r'详|参考|参阅',text[max(0,match.start()-30):match.start()]):continue
                    link=urlsplit(match[2])
                    if link.scheme or link.netloc or not link.path:continue
                    target=str((Path(evidence['original_path']).parent/unquote(link.path)).resolve())
                    j=by_path.get(target)
                    if j is None or self.rows[j]['source_version']!=evidence['source_version']:continue
                    if any(row['doc_id']==self.rows[j]['doc_id'] for row in output):continue
                    same=[i for i in eligible if self.rows[i]['doc_id']==self.rows[j]['doc_id']]
                    j=min(same,key=lambda i:min((p['group_rank'] for p in provenance[i] if p['subquery_id'] in basics),default=999))
                    output.append({**self.rows[j],'rank':len(output)+1,'score':dict(ranking)[j],
                        'retrieval_mode':mode,'build_id':self.manifest['build_id'],'retrieval_trace':provenance[j],
                        'fusion':'rrf_candidate_explicit_reference','selection_reason':'explicit_reference',
                        'context_reference':{'from_chunk_id':evidence['chunk_id'],'label':match[1],'target':match[2]},
                        'reference_root':True})
                    return output
        return output

    def search(self, query: str, version: str | None = None, top_k: int = 5, mode: str = "dense") -> list[dict]:
        from .query_understanding import understand_query
        plan = understand_query(query, version)
        if plan["status"] != "ready":
            raise ValueError(plan["clarification"])
        return self.search_plan(plan, top_k, mode)
