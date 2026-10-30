"""本地递归读取；保留来源，不能把目录版本当作功能支持范围。"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import yaml

from .common import config, sha256_file, sha256_text, write_json, write_jsonl
from .markdown import FENCE, clean_with_mapping, source_range, structure

DOCS_PATH = Path("i18n/zh-CN/docusaurus-plugin-content-docs")
VERSION = re.compile(r"(?<![a-zA-Z0-9_.])(?:[1-4]\.\d{1,2}(?:\.\d{1,3})?|[1-4]\.x)(?:\+|\.x)?(?![a-zA-Z0-9_.])")


def front_matter(raw: str) -> tuple[dict, str]:
    match = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", raw, re.S)
    if not match:
        return {}, raw
    value = yaml.safe_load(match.group(1)) or {}
    if not isinstance(value, dict):
        raise ValueError("front matter 必须为 JSON/YAML 对象")
    return value, raw[match.end():]


def clean_markdown(body: str) -> tuple[str, list[str]]:
    parsed = clean_with_mapping(body)
    return parsed["cleaned_text"], sorted({w["kind"] for w in parsed["parse_warnings"]})


def version_statements(text: str) -> list[dict]:
    statements = []
    for number, line in enumerate(text.splitlines(), 1):
        found = VERSION.findall(re.sub(r"(Doris|version)(?=\d)", r"\1 ", line, flags=re.I))
        if found and re.search(r"版本|支持|不支持|自|起|之前|之后|以上|以下|移除|默认|Doris", line, re.I):
            statements.append({"line": number, "versions": sorted(set(found)), "text": line.strip()})
    return statements


def notices(text: str) -> list[str]:
    sections, scopes = structure(text, "")
    first_heading = next((s["start"] for s in sections if s["level"] > 1), len(text))
    return [s["text"] for s in scopes if s["kind"] == "admonition"
            and not s["parent_scope_ids"] and s["end"] <= first_heading]


def load_document(path: Path, version: str, root: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    metadata, body = front_matter(raw)
    parsed = clean_with_mapping(body, raw[:len(raw) - len(body)].count("\n") + 1)
    cleaned = parsed["cleaned_text"]
    warnings = sorted({w["kind"] for w in parsed["parse_warnings"]})
    relative = path.relative_to(root / DOCS_PATH / f"version-{version}").as_posix()
    repo_path = path.relative_to(root).as_posix()
    title = str(metadata.get("title") or path.stem)
    route = str(metadata.get("slug") or Path(relative).with_suffix("").as_posix()).strip("/")
    doc_id = f"doris:zh-CN:{version}:{relative}"
    section_nodes, scopes = structure(cleaned, title, doc_id)
    for node in section_nodes:
        node.update(source_range(parsed, node["start"], node["subtree_end"]))
    for scope in scopes:
        scope.update(source_range(parsed, scope["start"], scope["end"]))
        if not scope["closed"]:
            parsed["parse_warnings"].append({"kind": "unclosed_" + scope["kind"],
                                              **source_range(parsed, scope["start"], scope["end"])})
    warnings = sorted({w["kind"] for w in parsed["parse_warnings"]})
    statements = version_statements(body)
    body_first_line = raw[:len(raw) - len(body)].count("\n") + 1
    for statement in statements:
        statement["line"] += body_first_line - 1
        statement["source_start_line"] = statement["source_end_line"] = statement["line"]
        mapped = next((m for m in parsed["source_map"]
                       if m["source_start_line"] <= statement["line"] <= m["source_end_line"]), None)
        statement["section_id"] = next((s["section_id"] for s in section_nodes
                                        if mapped and s["start"] <= mapped["start"] < s["end"]), None)
        statement["scope_ids"] = [s["scope_id"] for s in scopes if mapped
                                  and s["start"] <= mapped["start"] < s["end"]]
    return {
        "doc_id": doc_id,
        **parsed, "sections": section_nodes, "scopes": scopes,
        "source": "apache_doris_local_website",
        "source_version": version,
        "locale": "zh-CN",
        "title": title,
        "topic": relative.split("/")[0],
        "topic_path": str(Path(relative).parent.as_posix()),
        "relative_path": relative,
        "repo_path": repo_path,
        "original_path": str(path.resolve()),
        "source_url": f"https://doris.apache.org/zh-CN/docs/{version}/{quote(route, safe='/')}",
        "source_sha256": sha256_file(path),
        "cleaned_sha256": sha256_text(cleaned),
        "cleaned_text": cleaned,
        "front_matter": metadata,
        "version_statements": statements,
        "document_version_notices": notices(cleaned),
        "applicability_status": "read_body_conditions" if statements else "not_explicitly_stated",
        "quality_warnings": warnings,
    }


def normalized(doc: dict) -> str:
    return re.sub(r"\s+", "", doc["cleaned_text"])


def select_history(all_docs: dict[str, list[dict]], target: int) -> tuple[list[dict], list[dict]]:
    """优先调查中确认的差异主题，再选变化证据；少量重复保留为对照。"""
    if not 200 <= target <= 400:
        raise ValueError("2.1 选择数量必须在 200～400")
    newer = {v: {d["relative_path"]: d for d in all_docs[v]} for v in ("4.x", "3.x")}
    core_prefixes = (
        "table-design/", "data-operate/", "admin-manual/config/", "admin-manual/data-admin/ccr/",
        "lakehouse/catalogs/", "query-acceleration/materialized-view/", "admin-manual/auth/",
    )
    controls = {
        "sql-manual/sql-functions/scalar-functions/numeric-functions/pi.md",
        "sql-manual/sql-functions/scalar-functions/numeric-functions/abs.md",
        "sql-manual/sql-functions/scalar-functions/encrypt-digest-functions/crc32.md",
        "admin-manual/open-api/fe-http/node-action.md",
        "admin-manual/open-api/fe-http/upload-action.md", "lakehouse/meta-cache.md",
    }
    manifest = []
    for doc in all_docs["2.1"]:
        path = doc["relative_path"]
        other = newer["3.x"].get(path) or newer["4.x"].get(path)
        similarity = None
        signals = 0
        if other:
            a, b = doc["cleaned_text"].splitlines(), other["cleaned_text"].splitlines()
            similarity = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
            changed = [line for line in difflib.unified_diff(a, b, n=0)
                       if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
            signals = sum(bool(re.search(r"默认|不支持|支持|限制|版本|[0-9]|`", line)) for line in changed)
        core = path.startswith(core_prefixes) or path in controls
        # 这是选集优先级，不把文本变化分数伪装为已核验的产品行为变化。
        score = (1000 if core else 0) + (40 if other is None else 30 * (1 - similarity)) + min(signals, 120)
        reason = "investigated_topic_group" if core else "version_only_path_candidate" if other is None else "changed_facts_candidate"
        if other and normalized(doc) == normalized(other):
            reason += ":duplicate_control_or_topic_context"
        manifest.append({"doc_id": doc["doc_id"], "relative_path": path, "topic": doc["topic"],
                         "source_sha256": doc["source_sha256"], "reason": reason,
                         "comparison_source_version": other["source_version"] if other else None,
                         "text_similarity": similarity, "changed_signal_lines": signals,
                         "selection_score": score, "selected": False})
    ranking = sorted(manifest, key=lambda row: (-row["selection_score"], row["relative_path"]))
    if sum(row["selection_score"] >= 1000 for row in ranking) > target:
        raise ValueError("必选主题超过选集数量，需要明确调整范围")
    selected = {row["doc_id"] for row in ranking[:target]}
    for rank, row in enumerate(ranking, 1):
        row["rank"] = rank
        row["selected"] = row["doc_id"] in selected
        if not row["selected"]:
            row["not_selected_reason"] = "outside_ranked_history_budget"
    return [d for d in all_docs["2.1"] if d["doc_id"] in selected], ranking


def write_parse_anomalies(documents: list[dict], data: Path) -> None:
    rows = []
    for doc in documents:
        warnings = doc["parse_warnings"][:]
        if not doc["cleaned_text"]:
            raw = Path(doc["original_path"]).read_text(encoding="utf-8-sig")
            warnings.append({"kind": "empty_cleaned_body", "source_start_line": 1,
                             "source_end_line": max(1, len(raw.splitlines()))})
        for warning in warnings:
            rows.append({"doc_id": doc["doc_id"], "original_path": doc["original_path"],
                         "source_version": doc["source_version"], "source_sha256": doc["source_sha256"],
                         **warning})
    write_jsonl(data / "manifests" / "parse_anomalies.jsonl", rows)


def import_documents() -> tuple[list[dict], dict]:
    cfg = config()
    root, data = Path(cfg["source_root"]), Path(cfg["data_dir"])
    all_docs = {}
    for version in ("4.x", "3.x", "2.1"):
        directory = root / DOCS_PATH / f"version-{version}"
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        # 借鉴参考项目 rglob(md/mdx) + UTF-8 的直接读取方式，不移动源文件。
        paths = sorted(p for p in directory.rglob("*") if p.suffix.lower() in (".md", ".mdx"))
        expected = cfg["expected_full_counts"].get(version)
        if expected and len(paths) != expected:
            raise ValueError(f"{version} 文档数 {len(paths)} 与已确认快照 {expected} 不符")
        all_docs[version] = [load_document(path, version, root) for path in paths]
    documents = all_docs["4.x"] + all_docs["3.x"] + all_docs["2.1"]
    write_jsonl(data / "processed" / "documents.jsonl", documents)
    audit = {"available_by_version": {v: len(rows) for v, rows in all_docs.items()},
             "selected_by_version": dict(Counter(d["source_version"] for d in documents)),
             "total_selected": len(documents),
             "empty_cleaned_documents": [d["doc_id"] for d in documents if not d["cleaned_text"]],
             "warning_documents": [{"doc_id": d["doc_id"], "warnings": d["parse_warnings"]}
                                   for d in documents if d["quality_warnings"]],
             "scope_policy": "full_4.x_3.x_2.1",
             "version_condition_documents": sum(bool(d["version_statements"]) for d in documents),
             "source_snapshot_sha256": sha256_text(json.dumps([(d["doc_id"], d["source_sha256"]) for d in documents]))}
    write_json(data / "manifests" / "import_audit.json", audit)
    write_parse_anomalies(documents, data)
    return documents, audit
