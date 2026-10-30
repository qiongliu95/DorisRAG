"""按第一轮的关联范围补取原文，预算不足显式记录，不伪装完整结构。"""
from __future__ import annotations

from pathlib import Path
import re

from .common import config, sha256_file
from .telemetry import event


def contained_evidence(rows: list[dict]) -> dict[str, list[str]]:
    """仅淘汰同版本、同可信来源、同作用域且所有送模正文均已被前项包含的证据。"""
    redundant, prior = {}, []
    referenced = {r.get('context_reference', {}).get('from_chunk_id') for r in rows}
    for row in rows:
        if (row.get('reference_root') or row.get('chunk_id') in referenced or not row.get('source_sha256')
                or row.get('context_status') != 'complete'):
            prior.append(row)
            continue
        same = [p for p in prior if p.get('doc_id') == row.get('doc_id')
                and p.get('source_version') == row.get('source_version')
                and p.get('source_sha256') == row['source_sha256'] and p.get('context_status') == 'complete'
                and p.get('tab_labels', []) == row.get('tab_labels', [])
                and p.get('scope_ids', []) == row.get('scope_ids', [])]
        blocks = [r['chunk_text'] for r in same] + [c['text'] for r in same for c in r.get('contexts', [])
                  if c.get('source_sha256') == row['source_sha256']]
        fragments = [row['chunk_text']] + [c['text'] for c in row.get('contexts', [])]
        # 版本和适用条件也是证据，不能因为正文相同而丢掉独立的限定事实。
        notices = lambda r: r.get('version_note', []) + r.get('document_version_notices', []) + [
            c['text'] for c in r.get('applicability_conditions', [])]
        prior_notices = [t for p in same for t in notices(p)]
        fragments += [t for t in notices(row) if t not in prior_notices]
        if same and all(not text.strip() or any(text.strip() in b for b in blocks) for text in fragments):
            redundant[row['chunk_id']] = [p['chunk_id'] for p in same]
        else:
            prior.append(row)
    return redundant


def priority_spans(parsed, row, lines):
    """用已校验的解析树定位召回作用域；限制同时保留紧邻的功能标题和版本前言。"""
    result = []
    sections = {s['section_id']: s for s in parsed['sections']}
    for anchor in row.get('context_anchors', []):
        role = anchor.get('role')
        if role not in ('foundation_scope', 'recalled_constraint'):
            continue
        for scope in parsed['scopes']:
            expected = 'TabItem' if role == 'foundation_scope' else 'admonition'
            if scope['scope_id'] not in anchor['scope_ids'] or scope['kind'] != expected or not scope['closed']:
                continue
            span = dict(scope)
            parent = sections.get(scope['section_id'])
            if role == 'recalled_constraint' and parent and not any(
                    s['parent_id'] == parent['section_id'] and s['source_start_line'] < scope['source_start_line']
                    for s in parsed['sections']) and sum(map(len,
                    lines[parent['source_start_line']-1:scope['source_end_line']])) <= 4000:
                # 只扩到本节首个条件块，不把其他子节的操作、版本条件串到一起。
                span['source_start_line'] = parent['source_start_line']
            result.append((role + ':' + (parent['label'] if role == 'recalled_constraint' and parent else scope['label']), span))
    return result


def enrich_context(rows: list[dict], query: str, max_chars: int = 14000) -> list[dict]:
    root = Path(config()["source_root"]).resolve()
    remaining, cache, result = max_chars, {}, []
    introductions = set()
    needs_rules = bool(re.search(r'条件|限制|比较|对比|失败|生效|支持|如何|怎么', query))
    rule_label=r'(?:使用|适用)?(?:条件|限制|场景)|使用索引|连接前的准备'
    reserved,parsed_cache,priorities={}, {}, {}
    priority_seen = set()
    if needs_rules or any(a.get('role') in ('foundation_scope', 'recalled_constraint')
                          for r in rows for a in r.get('context_anchors', [])):
        from .documents import load_document
        # 先为各个可信来源的适用条件留预算，避免前面的长父章节挤掉后续文档的关键限制。
        for original in rows:
            if not original.get('original_path') or not original.get('source_sha256'):continue
            path=Path(original['original_path']).resolve()
            if not path.is_relative_to(root) or path in parsed_cache:continue
            try:
                if path not in cache:cache[path]=(sha256_file(path),path.read_text(encoding='utf-8-sig').splitlines(keepends=True))
                digest,lines=cache[path]
                if digest!=original['source_sha256']:continue
                parsed=load_document(path,original['source_version'],root);parsed_cache[path]=parsed
                scoped = [entry for r in rows if r.get('original_path') == original['original_path']
                          for entry in priority_spans(parsed, r, lines)]
                unique = {}
                for kind, span in scoped:
                    unique.setdefault((span['source_start_line'], span['source_end_line']), (kind, span))
                priorities[path] = list(unique.values())
                rules = [s for s in parsed['sections'] if needs_rules and not s.get('scope_ids')
                         and re.fullmatch(rule_label, s['label'])]
                line_numbers = {n for s in rules + [s for _, s in priorities[path]]
                                for n in range(s['source_start_line'], s['source_end_line'] + 1)}
                size = sum(len(lines[n-1]) for n in line_numbers)
                if size:reserved[path]=min(size,4000)
            except (OSError,ValueError,KeyError):
                # 来源错误仍由下面的原有校验分支逐项标记，不用其他文件的内容兜底。
                continue
        if sum(reserved.values())>max_chars:
            share=max_chars//len(reserved)
            reserved={p:min(size,share) for p,size in reserved.items()}
    # 显式引用的基础规则先留预算，结果仍按原证据编号展示。
    for original in sorted(rows,key=lambda r:not r.get('reference_root',False)):
        row = {**original, "contexts": [], "context_status": "not_available", "context_issues": []}
        result.append(row)
        if not row.get("original_path") or not row.get("source_sha256"):
            row["context_issues"].append("missing_source_metadata")
            continue
        path = Path(row["original_path"]).resolve()
        if not path.is_relative_to(root):
            row["context_status"] = "untrusted_source"
            row["context_issues"].append("source_outside_allowed_root")
            continue
        try:
            if path not in cache:
                cache[path] = (sha256_file(path), path.read_text(encoding="utf-8-sig").splitlines(keepends=True))
            digest, lines = cache[path]
            if digest != row["source_sha256"]:
                row["context_status"] = "source_changed"
                row["context_issues"].append("source_sha256_mismatch")
                continue
            reserved.pop(path,None)
            spans = [("related", row.get("related_context") or row.get("section_context"))]
            from .documents import load_document
            parsed = parsed_cache.get(path)
            needs_concept = bool(re.search(r'什么是|是什么|概念|含义', query))
            if needs_rules or needs_concept or row.get('context_anchors') or row.get('reference_root') or row.get('tab_labels'):
                parsed = parsed or load_document(path, row['source_version'], root)
                node = next((s for s in parsed['sections'] if s['section_id'] == row.get('parent_section_id')), None)
                if node and not row.get('tab_labels') and re.search(r'\d+\.\d+.*(?:之前|之后|以下|以上|起)', node['label']):
                    # 版本分支的首条命中不能代替整套统计/行为规则；同页补基础节和所属主节，保留前后版本条件。
                    by_id = {s['section_id']: s for s in parsed['sections']}
                    family = node
                    while by_id.get(family['parent_id'], {}).get('parent_id') is not None:
                        family = by_id[family['parent_id']]
                    top = next(s for s in parsed['sections'] if s['parent_id'] is None)
                    foundation = next((s for s in parsed['sections'] if s['parent_id'] == top['section_id']), None)
                    spans = ([('version_foundation', foundation)] if foundation else []) + [('version_family', family)] + spans
                if needs_concept and path not in introductions:
                    # 概念题补页面引言，避免只命中方案小节时丢失主体说明；不补整篇长文档。
                    top = next(s for s in parsed['sections'] if s['parent_id'] is None)
                    children = [s['source_start_line'] for s in parsed['sections'] if s['parent_id'] == top['section_id']]
                    end = min(children) - 1 if children else top['source_end_line']
                    if end >= top['source_start_line']:
                        spans.insert(0, ('introduction', {'source_start_line': top['source_start_line'], 'source_end_line': end}))
                    introductions.add(path)
                # 文档级适用条件不能因命中示例或参数小节而丢失；Tab内条件仍留在自己的作用域。
                if needs_rules:
                    rules = [("applicability", s) for s in parsed['sections'] if not s.get('scope_ids')
                             and re.fullmatch(rule_label, s['label'])]
                    spans = rules + spans
                # 当前命中的Tab也要补完整作用域；入口说明本身可能只有一行，不能只补其他候选。
                anchors=[{'scope_ids':row.get('scope_ids',[])}]+row.get('context_anchors', [])
                for anchor in anchors:
                    scopes = [s for s in parsed['scopes'] if s['scope_id'] in anchor['scope_ids']
                              and s['kind']=='TabItem' and s['closed']]
                    spans.extend(('tab:'+s['label'], s) for s in scopes)
                    if not scopes and anchor.get('source_span'):
                        spans.append(('subquery:'+anchor['subquery_id'],anchor['source_span']))
                if row.get('reference_root'):
                    spans.insert(0,('referenced_document',next(s for s in parsed['sections'] if s['parent_id'] is None)))
            if sum(map(len, lines)) <= 4000 and row.get("parent_section_id") and not row.get("tab_labels"):
                # 短函数页的描述、语法和示例通常是兄弟小节；回取真实父节点才能带回示例结果。
                parsed = parsed or load_document(path, row["source_version"], root)
                node = next((s for s in parsed["sections"] if s["section_id"] == row["parent_section_id"]), None)
                parent = next((s for s in parsed["sections"] if node and s["section_id"] == node["parent_id"]), None)
                if parent:
                    spans.insert(0, ("parent_section", parent))
            if row.get("requires_parent_context") or any(term in query for term in ("完整", "条件", "限制", "步骤", "为什么", "失败")):
                spans.append(("section", row.get("section_context")))
            # 同源重叠范围合并后读取，避免片段、关联块及父节重复占用上下文预算。
            merged = []
            for kind, span in spans:
                if not span:
                    continue
                a,b=span['source_start_line'],span['source_end_line']
                for n,(old_kind,old) in enumerate(merged):
                    x,y=old['source_start_line'],old['source_end_line']
                    if a<=y and x<=b:
                        merged[n]=(old_kind,{'source_start_line':min(a,x),'source_end_line':max(b,y)})
                        break
                else:
                    merged.append((kind,span))
            spans = merged
            if row.get('split_kind') == 'oversize_table':
                # 切片可能只含一行的前半段；先核验完整命中原文行，不能让大表前缀耗尽预算。
                spans.insert(0, ('selected_table_row', {'source_start_line':row['source_start_line'],
                                                      'source_end_line':row['source_end_line']}))
            if path not in priority_seen:
                protected = priorities.get(path, [])
                priority_seen.add(path)
                # 在原有预算内先取必要作用域；不能被之后合并的长父节吞掉优先级。
                exact = {(s['source_start_line'], s['source_end_line']) for _, s in protected}
                spans = protected + [(kind, s) for kind, s in spans
                                     if (s['source_start_line'], s['source_end_line']) not in exact]
                if protected:
                    event('augmentation.priority_spans', [{'kind': kind,
                        'source_start_line': s['source_start_line'], 'source_end_line': s['source_end_line']}
                        for kind, s in protected], chunk_id=row.get('chunk_id'), count=len(protected))
            seen = set()
            for kind, span in spans:
                if not span:
                    continue
                a, b = span["source_start_line"], span["source_end_line"]
                if not 1 <= a <= b <= len(lines):
                    raise ValueError("原文范围无效")
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                allowance = min(max(0,remaining-sum(reserved.values())), 4000)
                event('augmentation.context_request', None, chunk_id=row.get('chunk_id'), kind=kind,
                      source_start_line=a, requested_source_end_line=b, allowance_chars=allowance)
                chosen, count = [], 0
                # 完整行优先。超长单行不切成貌似完整的 SQL/错误字符串。
                for line in lines[a - 1:b]:
                    if count + len(line) > allowance:
                        break
                    chosen.append(line)
                    count += len(line)
                complete = len(chosen) == b - a + 1
                if chosen:
                    row["contexts"].append({"kind": kind, "text": "".join(chosen),
                        "source_start_line": a, "source_end_line": a + len(chosen) - 1,
                        "requested_source_end_line": b, "complete": complete,
                        "source_sha256": digest, "parent_section_id": row.get("parent_section_id")})
                    remaining -= count
                    event('augmentation.context_fetched', None, chunk_id=row.get('chunk_id'), kind=kind,
                          source_start_line=a, source_end_line=a + len(chosen) - 1, complete=complete,
                          chars=count, source_sha256=digest)
                if not complete:
                    row["context_issues"].append(kind + "_context_budget_incomplete")
            header = row.get("table_header", "")
            if header and not any(header in c["text"] for c in row["contexts"]):
                if len(header) <= min(max(0,remaining-sum(reserved.values())), 4000):
                    row["contexts"].append({"kind": "parsed_table_header", "text": header,
                        "source_start_line": row["block_refs"][0]["source_start_line"],
                        "source_end_line": row["block_refs"][0]["source_end_line"],
                        "complete": True, "source_sha256": digest, "origin": "parsed_table_header"})
                    remaining -= len(header)
                else:
                    row["context_issues"].append("table_header_budget_incomplete")
            row["context_status"] = "partial" if row["context_issues"] else "complete" if row["contexts"] else "not_available"
        except (OSError, ValueError, KeyError) as error:
            row["context_status"] = "source_error"
            row["context_issues"].append(type(error).__name__)
    return sorted(result,key=lambda r:r.get('rank',0))
