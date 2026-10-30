"""以章节和容器为边界切分；完整跨度与关联上下文均可回溯原文。"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .common import config, embedding_text, sha256_file, sha256_text, tokens, write_json, write_jsonl
from .documents import version_statements
from .markdown import HEADING, closes, fence_match, scan_lines, source_range, structure


def sections(text: str, title: str) -> list[dict]:
    return [s for s in structure(text, title)[0] if s["end"] > s["start"]]


def blocks(text: str, start: int, end: int) -> list[dict]:
    """表格、列表和代码单独成块，容器边界不与前后内容混装。"""
    result, begin, kind, marker = [], start, None, None
    def emit(stop):
        nonlocal begin, kind, marker
        if stop > begin:
            if not text[begin:stop].strip() and result:
                result[-1]["end"] = stop
            else:
                result.append({"start": begin, "end": stop, "kind": kind or "text", "fence": marker})
        begin, kind, marker = stop, None, None
    for item in scan_lines(text[start:end]):
        a, b, line = start + item["start"], start + item["end"], item["text"]
        if item["opening"]:
            emit(a)
            kind, marker = "code", item["fence"]
        elif item["warning"] == "unclosed_fence_recovered_at_heading":
            emit(a)
        if item["code"]:
            if item["closing"]:
                emit(b)
            continue
        if HEADING.match(line):
            emit(a)
            kind = "heading"
            emit(b)
            continue
        if re.match(r"^\s*(?:</?(?:Tabs|TabItem)\b|:{3,})", line):
            emit(a)
            kind = "container"
            emit(b)
            continue
        detected = "table" if line.lstrip().startswith("|") else "list" if re.match(r"^\s*(?:[-*+] |\d+[.)] )", line) else "text"
        if kind == "table" and detected != "table" or detected == "table" and kind not in (None, "table"):
            emit(a)
        if line.strip():
            kind = kind or detected
        if not line.strip():
            emit(b)
    emit(end)
    return result


def make_row(doc: dict, text: str, path: list[str], start: int, end: int, rendered: str | None = None) -> dict:
    keys = ("doc_id", "source", "source_version", "locale", "title", "topic", "topic_path",
            "relative_path", "repo_path", "original_path", "source_url", "source_sha256",
            "cleaned_sha256", "document_version_notices", "applicability_status", "quality_warnings")
    row = {key: doc[key] for key in keys}
    labels = [s["label"] for s in doc.get("scopes", []) if s["kind"] == "TabItem"
              and s["start"] <= start < s["end"]]
    row.update(section_path=" > ".join(path), section_paths=[" > ".join(path)], tab_labels=labels,
               start_char=start, end_char=end, chunk_text=rendered if rendered is not None else text[start:end],
               split_kind="structure", source_span_sha256=sha256_text(text[start:end]))
    return row


def split_document(doc: dict, tok, budget: int) -> list[dict]:
    text = doc["cleaned_text"]
    nodes, scopes = structure(text, doc["title"], doc["doc_id"])
    for scope in scopes:
        scope.update(source_range(doc, scope["start"], scope["end"]))
    doc = {**doc, "scopes": scopes}
    rows, all_blocks = [], []
    target = min(config().get("target_body_tokens", 340), budget)
    for section in nodes:
        section_blocks = blocks(text, section["start"], section["end"])
        for block in section_blocks:
            block.update(block_id=f"{doc['doc_id']}#block-{block['start']}", section_id=section["section_id"],
                         tab_scope_ids=[s["scope_id"] for s in scopes if s["kind"] == "TabItem"
                                        and s["start"] <= block["start"] < s["end"]],
                         **source_range(doc, block["start"], block["end"]))
        all_blocks.extend(section_blocks)
        current = None
        for block in section_blocks:
            row = make_row(doc, text, section["path"], block["start"], block["end"])
            if current:
                joined = make_row(doc, text, section["path"], current["start_char"], block["end"])
                same_scope = current["tab_labels"] == row["tab_labels"] and block["kind"] != "container"
                # 目标是软预算；相邻 SQL/表格与说明可在硬预算内作为完整证据保留。
                related = block["kind"] in ("code", "table") or current.get("last_kind") in ("code", "table")
                if (same_scope and tokens(tok, embedding_text(joined)) <= budget
                        and (tokens(tok, joined["chunk_text"]) <= target or related)):
                    current = {**joined, "last_kind": block["kind"]}
                    continue
                rows.append(current)
                current = None
            if tokens(tok, embedding_text(row)) <= budget:
                current = {**row, "last_kind": block["kind"]}
                if block["kind"] == "container":
                    rows.append(current)
                    current = None
                continue
            start = block["start"]
            block_lines = text[start:block["end"]].rstrip().splitlines(keepends=True)
            marker = block.get("fence") or "```"
            closed = len(block_lines) > 1 and closes(fence_match(block_lines[-1]), marker)
            header = ""
            if block["kind"] == "table" and len(block_lines) > 1 and re.search(r"\|[\s:|-]+\|", block_lines[1]):
                header = "".join(block_lines[:2])
            # 优先 SQL 语句、空行及步骤边界；兜底仍逐行，再到不可避免的单行分片。
            logical = [m.end() for m in re.finditer(r";[ \t]*\n|\n[ \t]*\n|(?=^\s*\d+[.)] )", text[start:block["end"]], re.M)]
            logical = [block["start"] + value for value in logical if value]
            def render(a, b, repeat_header=True):
                piece = text[a:b]
                if block["kind"] == "code":
                    if a > block["start"]:
                        piece = block_lines[0].rstrip("\r\n") + "\n" + piece
                    if b < block["end"] or not closed:
                        piece += "\n" + marker
                elif a > block["start"] and header and repeat_header:
                    piece = header + piece
                return piece
            while start < block["end"]:
                repeat = True
                # 宽表头不强塞入每片，必须显式标识并提供完整表头原文引用。
                if header and start > block["start"]:
                    probe = make_row(doc, text, section["path"], start, start + 1, render(start, start + 1))
                    repeat = tokens(tok, embedding_text(probe)) < budget - 16
                low, high, best = start + 1, block["end"], start
                while low <= high:
                    middle = (low + high) // 2
                    candidate = make_row(doc, text, section["path"], start, middle, render(start, middle, repeat))
                    if tokens(tok, embedding_text(candidate)) <= budget:
                        best, low = middle, middle + 1
                    else:
                        high = middle - 1
                if best == start:
                    raise ValueError(f"标题/结构开销超过预算: {doc['doc_id']}")
                if best < block["end"]:
                    candidates = [n for n in logical if start < n <= best]
                    if candidates and candidates[-1] > start + (best - start) // 3:
                        best = candidates[-1]
                    else:
                        newline = text.rfind("\n", start, best)
                        if newline > start:
                            best = newline + 1
                piece = make_row(doc, text, section["path"], start, best, render(start, best, repeat))
                piece.update(split_kind="oversize_" + block["kind"],
                             requires_parent_context=True,
                             split_inside_line=bool(start > block["start"] and text[start - 1] != "\n"
                                                    or best < block["end"] and text[best - 1] != "\n"),
                             table_header=header, table_header_repeated=bool(header and repeat and start > block["start"]))
                rows.append(piece)
                start = best
        if current:
            rows.append(current)

    position = 0
    for number, row in enumerate(rows, 1):
        if row["start_char"] != position:
            raise ValueError(f"正文覆盖不连续: {doc['doc_id']}")
        position = row["end_char"]
        section = next(s for s in nodes if s["start"] <= row["start_char"] < s["end"])
        included = [b for b in all_blocks if b["start"] < row["end_char"] and b["end"] > row["start_char"]]
        active_scopes = [s for s in scopes if s["start"] <= row["start_char"] < s["end"]]
        ancestors, owner = set(), section
        while owner:
            ancestors.add(owner["section_id"])
            owner = next((s for s in nodes if s["section_id"] == owner.get("parent_id")), None)
        section_scopes = [s for s in scopes if s["kind"] == "admonition" and s["section_id"] in ancestors
                          and s["start"] < row["end_char"]
                          and all(any(a["scope_id"] == pid for a in active_scopes) for pid in s["parent_scope_ids"])]
        conditions = list({s["scope_id"]: s for s in active_scopes + section_scopes}.values())
        local_statements = []
        current_tabs = {s["scope_id"] for s in active_scopes if s["kind"] == "TabItem"}
        for statement in doc.get("version_statements", []):
            statement_tabs = {s["scope_id"] for s in scopes if s["kind"] == "TabItem"
                              and s["scope_id"] in statement.get("scope_ids", [])}
            if statement.get("section_id") in ancestors and statement_tabs <= current_tabs:
                local_statements.append(statement)
        types = list(dict.fromkeys(b["kind"] for b in included))
        related_ids = []
        for block in included:
            i = all_blocks.index(block)
            for neighbor in all_blocks[max(0, i - 1):i + 2]:
                if (neighbor["section_id"] == block["section_id"] and neighbor["kind"] != "container"
                        and neighbor["tab_scope_ids"] == block["tab_scope_ids"]):
                    related_ids.append(neighbor["block_id"])
        context_start, context_end = section["start"], section["subtree_end"]
        for scope in active_scopes:
            if scope["kind"] == "TabItem":
                context_start, context_end = max(context_start, scope["start"]), min(context_end, scope["end"])
        related_blocks = [b for b in all_blocks if b["block_id"] in related_ids] or included
        row.pop("last_kind", None)
        row.update(chunk_id=f"{doc['doc_id']}#chunk-{number:04d}", chunk_index=number,
                   embedding_tokens=tokens(tok, embedding_text(row)),
                   parent_section_id=section["section_id"],
                   section_context={"section_id": section["section_id"],
                                    **source_range(doc, context_start, context_end)},
                   block_refs=included, related_block_ids=list(dict.fromkeys(related_ids)),
                   knowledge_unit_id=section["section_id"] + ":" + ",".join(sorted(current_tabs)),
                   related_context=source_range(doc, min(b["start"] for b in related_blocks),
                                                max(b["end"] for b in related_blocks)),
                   scope_ids=[s["scope_id"] for s in active_scopes],
                   applicability_conditions=[{k: v for k, v in s.items() if k != "heading_stack"}
                                             for s in conditions if s["kind"] == "admonition"],
                   version_statements=local_statements,
                   version_note=[s["text"] for s in conditions if s["kind"] == "admonition"] + [s["text"] for s in local_statements],
                   content_type=types[0] if len(types) == 1 else "mixed", content_types=types,
                   has_code_block="code" in types, **source_range(doc, row["start_char"], row["end_char"]))
        row["source_content"] = {"path": row["original_path"], "sha256": row["source_sha256"],
                                 "start_line": row["source_start_line"], "end_line": row["source_end_line"]}
        row["retrieval_text"] = embedding_text(row)
        if row["embedding_tokens"] > budget:
            raise ValueError("Embedding 输入超限")
    if position != len(text):
        raise ValueError(f"正文丢失: {doc['doc_id']}")
    return rows


def source_context(row: dict, context: str = "section") -> str:
    """供后续阶段受控回取使用；本轮不接入检索或回答流程。"""
    path = Path(row["original_path"])
    if sha256_file(path) != row["source_sha256"]:
        raise ValueError("源文件已变化，拒绝回取错误快照")
    if context not in ("section", "related", "source"):
        raise ValueError("未知的上下文回取范围")
    span = row[context + "_context"] if context != "source" else row
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    return "".join(lines[span["source_start_line"] - 1:span["source_end_line"]])


def prepare_chunks(documents: list[dict], tok) -> dict:
    cfg, rows, doc_audits = config(), [], []
    budget = min(cfg["max_embedding_tokens"], tok.model_max_length)
    for number, doc in enumerate(documents, 1):
        chunks = split_document(doc, tok, budget)
        rows.extend(chunks)
        doc_audits.append({"doc_id": doc["doc_id"], "source_version": doc["source_version"],
                           "original_path": doc["original_path"], "source_sha256": doc["source_sha256"],
                           "cleaned_characters": len(doc["cleaned_text"]), "chunks": len(chunks),
                           "covered_characters": sum(c["end_char"] - c["start_char"] for c in chunks),
                           "status": "indexed_content" if chunks else "empty_cleaned_body"})
        if number % 200 == 0 or number == len(documents):
            print(f"Chunking {number}/{len(documents)}, chunks={len(rows)}", flush=True)
    audit = {"selected_documents": len(documents), "documents_with_chunks": sum(x["chunks"] > 0 for x in doc_audits),
             "chunks": len(rows), "chunks_by_version": dict(Counter(x["source_version"] for x in rows)),
             "embedding_token_limit": budget, "target_body_tokens": cfg.get("target_body_tokens", 340),
             "max_embedding_tokens": max((x["embedding_tokens"] for x in rows), default=0),
             "over_budget_chunks": sum(x["embedding_tokens"] > budget for x in rows),
             "split_kinds": dict(Counter(x["split_kind"] for x in rows)),
             "missing_characters": sum(x["cleaned_characters"] - x["covered_characters"] for x in doc_audits),
             "zero_chunk_documents": [x for x in doc_audits if not x["chunks"]]}
    data = Path(cfg["data_dir"])
    write_jsonl(data / "processed" / "chunks.jsonl", rows)
    write_jsonl(data / "manifests" / "document_coverage.jsonl", doc_audits)
    write_json(data / "manifests" / "chunk_audit.json", audit)
    return audit
