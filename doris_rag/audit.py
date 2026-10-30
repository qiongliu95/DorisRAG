"""独立检查源文档、清洗和 chunk 产物，避免以脚本退出码替代完整性检查。"""
from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from pathlib import Path

from .common import config, embedding_text, read_jsonl, sha256_file, sha256_text, tokenizer, tokens, write_json
from .documents import front_matter
from .markdown import scan_lines, source_range
from .html_tables import TableParser


def code_payloads(text: str) -> list[str]:
    lines, result = [], []
    for item in scan_lines(text):
        if item["opening"] or item["warning"] == "unclosed_fence_recovered_at_heading":
            if lines:
                result.append("".join(lines).strip())
            lines = []
        elif item["closing"]:
            result.append("".join(lines).strip())
            lines = []
        elif item["code"]:
            lines.append(item["text"])
    if lines:
        result.append("".join(lines).strip())
    return [value for value in result if value]


def verify() -> dict:
    cfg = config()
    data = Path(cfg["data_dir"])
    docs = read_jsonl(data / "processed" / "documents.jsonl")
    chunks = read_jsonl(data / "processed" / "chunks.jsonl")
    by_document = defaultdict(list)
    for chunk in chunks:
        by_document[chunk["doc_id"]].append(chunk)
    errors, code_missing, inline_missing, table_missing = [], [], [], []
    code_count = cell_count = 0
    for doc in docs:
        path = Path(doc["original_path"])
        if sha256_file(path) != doc["source_sha256"]:
            errors.append({"doc_id": doc["doc_id"], "error": "source_changed"})
        _, raw_body = front_matter(path.read_text(encoding="utf-8-sig"))
        outside = [item["text"] for item in scan_lines(raw_body) if not item["code"]]
        for table in re.findall(r"<table\b[^>]*>\s*(?=<(?:tr|thead|tbody|caption|colgroup)\b).*?</table\s*>", "".join(outside), re.I | re.S):
            parser = TableParser()
            parser.feed(table)
            cleaned = re.sub(r"\s+", "", doc["cleaned_text"].replace("\\|", "|"))
            for row in parser.rows:
                for value, _, _ in row:
                    if value:
                        cell_count += 1
                        if re.sub(r"\s+", "", value) not in cleaned:
                            table_missing.append({"doc_id": doc["doc_id"], "value": value})
        for payload in code_payloads(raw_body):
            code_count += 1
            if payload not in doc["cleaned_text"]:
                code_missing.append({"doc_id": doc["doc_id"], "payload_sha256": sha256_text(payload)})
        # 这是复核队列，不自动把注释/组件中的标识符差异判为正文丢失。
        for value in set(re.findall(r"(?<!`)`([^`\n]+)`(?!`)", raw_body)):
            if value not in doc["cleaned_text"] and html.unescape(value) not in doc["cleaned_text"]:
                inline_missing.append({"doc_id": doc["doc_id"], "value": value})
        position = 0
        for row in sorted(by_document[doc["doc_id"]], key=lambda row: row["start_char"]):
            if row["start_char"] != position:
                errors.append({"doc_id": doc["doc_id"], "error": "noncontiguous_span"})
            if sha256_text(doc["cleaned_text"][row["start_char"]:row["end_char"]]) != row["source_span_sha256"]:
                errors.append({"doc_id": doc["doc_id"], "error": "source_span_mismatch"})
            position = row["end_char"]
        if position != len(doc["cleaned_text"]):
            errors.append({"doc_id": doc["doc_id"], "error": "missing_cleaned_body"})
        node_ids = {node["section_id"] for node in doc["sections"]}
        block_ids = {ref["block_id"] for row in by_document[doc["doc_id"]] for ref in row["block_refs"]}
        source_lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        scope_ids = {scope["scope_id"] for scope in doc["scopes"]}
        for node in doc["sections"]:
            if node["parent_id"] is not None and node["parent_id"] not in node_ids:
                errors.append({"doc_id": doc["doc_id"], "error": "dangling_parent_section"})
        for row in by_document[doc["doc_id"]]:
            expected = source_range(doc, row["start_char"], row["end_char"])
            if (row["parent_section_id"] not in node_ids or
                    any(row[key] != value for key, value in expected.items()) or
                    not 1 <= row["source_start_line"] <= row["source_end_line"] <= len(source_lines)):
                errors.append({"chunk_id": row["chunk_id"], "error": "invalid_source_or_parent"})
            if any(value not in block_ids for value in row["related_block_ids"]):
                errors.append({"chunk_id": row["chunk_id"], "error": "dangling_related_block"})
            if not set(row["scope_ids"]) <= scope_ids:
                errors.append({"chunk_id": row["chunk_id"], "error": "dangling_scope"})
            expected_tabs = [scope["label"] for scope in doc["scopes"] if scope["kind"] == "TabItem"
                             and scope["start"] <= row["start_char"] < scope["end"]]
            if row["tab_labels"] != expected_tabs:
                errors.append({"chunk_id": row["chunk_id"], "error": "tab_binding_mismatch"})
            if embedding_text(row) != row["retrieval_text"]:
                errors.append({"chunk_id": row["chunk_id"], "error": "retrieval_text_mismatch"})
        mapped_position = 0
        for mapping in doc["source_map"]:
            if mapping["start"] != mapped_position:
                errors.append({"doc_id": doc["doc_id"], "error": "source_map_gap"})
            mapped_position = mapping["end"]
            if mapping["transform"] == "identity":
                original = "".join(source_lines[mapping["source_start_line"] - 1:mapping["source_end_line"]])
                cleaned_piece = doc["cleaned_text"][mapping["start"]:mapping["end"]]
                if cleaned_piece.strip() != original.strip():
                    errors.append({"doc_id": doc["doc_id"], "error": "identity_mapping_mismatch", **mapping})
        if mapped_position != len(doc["cleaned_text"]):
            errors.append({"doc_id": doc["doc_id"], "error": "source_map_incomplete"})
    tok = tokenizer()
    measured = [tokens(tok, embedding_text(row)) for row in chunks]
    if any(n > min(cfg["max_embedding_tokens"], tok.model_max_length) for n in measured):
        errors.append({"error": "embedding_overflow"})
    if Counter(d["source_version"] for d in docs) != cfg["expected_full_counts"]:
        errors.append({"error": "incomplete_source_scope"})
    if len({c["chunk_id"] for c in chunks}) != len(chunks):
        errors.append({"error": "duplicate_chunk_ids"})
    result = {"chunks_sha256": sha256_file(data / "processed" / "chunks.jsonl"),
              "documents_sha256": sha256_file(data / "processed" / "documents.jsonl"),
              "config_sha256": sha256_file(Path(__file__).resolve().parent.parent / "config.json"),
              "documents": len(docs), "chunks": len(chunks), "source_hashes_checked": len(docs),
              "original_code_blocks_checked": code_count, "missing_code_payloads": code_missing,
              "html_cells_checked": cell_count, "missing_html_cells": table_missing,
              "inline_review_queue": inline_missing, "integrity_errors": errors,
              "max_embedding_tokens": max(measured),
              "documents_by_version": dict(Counter(d["source_version"] for d in docs)),
              "unique_chunk_ids": len({c["chunk_id"] for c in chunks}),
              "passed": not errors and not code_missing and not table_missing}
    write_json(data / "manifests" / "verification.json", result)
    if not result["passed"]:
        raise ValueError("完整性检查失败，请查看 data/manifests/verification.json")
    return result
