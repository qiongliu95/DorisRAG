"""保守解析 Markdown/MDX；结构识别与原文映射共用同一围栏状态机。"""
from __future__ import annotations

import re

from .html_tables import table_to_markdown

FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\r\n]*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")


def fence_match(line: str):
    found = FENCE.match(line.rstrip("\r\n"))
    # CommonMark 的反引号信息串不能再含反引号；整行行内代码不是围栏。
    if found and not (found[1][0] == "`" and "`" in found[2]):
        return found
    return None


def closes(found, marker: str) -> bool:
    return bool(found and found[1][0] == marker[0] and len(found[1]) >= len(marker)
                and not found[2].strip())


def scan_lines(text: str):
    lines = text.splitlines(keepends=True)
    fence, offset, recover = None, 0, False
    for index, line in enumerate(lines):
        found, warning = fence_match(line), None
        # 无闭合符时才允许在标题处恢复。完整围栏中的 Markdown 标题保持为代码。
        if fence and recover and HEADING.match(line):
            fence, recover = None, False
            warning = "unclosed_fence_recovered_at_heading"
        opening = not fence and found is not None
        code = bool(fence or opening)
        marker = fence or (found[1] if opening else None)
        if opening:
            fence = found[1]
            recover = not any(closes(fence_match(value), fence) for value in lines[index + 1:])
            if recover:
                warning = "unclosed_source_fence"
        ending = not opening and fence and closes(found, fence)
        yield {"start": offset, "end": offset + len(line), "line": index + 1,
               "text": line, "code": code, "opening": opening, "closing": bool(ending),
               "fence": marker, "warning": warning}
        if ending:
            fence, recover = None, False
        offset += len(line)


def clean_with_mapping(body: str, first_line: int = 1) -> dict:
    """仅删除确认的注释/import 噪声；未知 HTML/组件保留供复核，不猜测其语义。"""
    scanned = list(scan_lines(body))
    parts, mapping, changes, warnings = [], [], [], []
    offset, index, in_comment = 0, 0, False
    while index < len(scanned):
        item = scanned[index]
        start_line = end_line = first_line + item["line"] - 1
        original, rendered, reason = item["text"], item["text"], None
        if item["warning"]:
            warnings.append({"kind": item["warning"], "source_start_line": start_line,
                             "source_end_line": end_line})
        if not item["code"]:
            # 只认完整的模块导入；export 和未知 JSX 可能含技术内容，原样保存。
            if re.match(r"^\s*import\s+.+?\s+from\s+['\"].+['\"];?\s*$", original):
                rendered, reason = "", "module_import"
            elif in_comment or original.lstrip().startswith("<!--"):
                in_comment = in_comment or "<!--" in original
                rendered = re.sub(r"<!--.*?-->", "", original)
                if "<!--" in rendered or in_comment and "<!--" not in original:
                    if "-->" in rendered:
                        rendered = rendered.split("-->", 1)[1]
                    else:
                        rendered = rendered.split("<!--", 1)[0] if "<!--" in rendered else ""
                in_comment = "-->" not in original
                reason = "html_comment"
            elif re.match(r"^\s*<table\b", original, re.I):
                # 表格占一个映射段，转换行追溯到整张原表，绝不伪造逐行对应。
                while "</table>" not in original.lower() and index + 1 < len(scanned):
                    index += 1
                    original += scanned[index]["text"]
                    end_line = first_line + scanned[index]["line"] - 1
                rendered = table_to_markdown(original)
                reason = "html_table_to_markdown"
                if rendered == original:
                    warnings.append({"kind": "html_table_unconverted_review",
                                     "source_start_line": start_line, "source_end_line": end_line})
            elif re.search(r"<(?!!--)/?(?!Tabs\b|TabItem\b)[A-Z][\w.]*|^\s*export\b", original):
                warnings.append({"kind": "preserved_mdx_review", "source_start_line": start_line,
                                 "source_end_line": end_line})
        if rendered:
            parts.append(rendered)
            mapping.append({"start": offset, "end": offset + len(rendered),
                            "source_start_line": start_line, "source_end_line": end_line,
                            "transform": reason or "identity"})
            offset += len(rendered)
        if reason:
            changes.append({"kind": reason, "source_start_line": start_line,
                            "source_end_line": end_line, "source_text": original})
        index += 1
    untrimmed = "".join(parts)
    left = len(untrimmed) - len(untrimmed.lstrip())
    right = len(untrimmed.rstrip())
    mapping = [{**m, "start": max(m["start"], left) - left,
                "end": min(m["end"], right) - left}
               for m in mapping if m["end"] > left and m["start"] < right]
    return {"cleaned_text": untrimmed.strip(), "source_map": mapping,
            "transformations": changes, "parse_warnings": warnings}


def source_range(doc: dict, start: int, end: int) -> dict:
    spans = [m for m in doc.get("source_map", []) if m["start"] < end and m["end"] > start]
    if not spans:
        text = doc["cleaned_text"]
        return {"source_start_line": text.count("\n", 0, start) + 1,
                "source_end_line": text.count("\n", 0, max(start, end - 1)) + 1}
    return {"source_start_line": min(m["source_start_line"] for m in spans),
            "source_end_line": max(m["source_end_line"] for m in spans)}


def structure(text: str, title: str, doc_id: str = "document") -> tuple[list[dict], list[dict]]:
    """章节节点与容器均以位置标识；同名标题/重复 Tab 不会混淆。"""
    sections, scopes, headings, active = [], [], [], []
    current = {"start": 0, "path": [title], "level": 0, "parent_id": None,
               "section_id": f"{doc_id}#root", "scope_ids": [], "label": title}
    sections.append(current)
    for item in scan_lines(text):
        if item["code"]:
            continue
        line = item["text"]
        heading = HEADING.match(line)
        tag = re.match(r"^\s*</?(Tabs|TabItem)\b[^>]*>", line)
        notice = re.match(r"^\s*(:{3,})([A-Za-z]+)?(?:\s+(.*))?\s*$", line)
        if tag or notice:
            closing = bool(tag and line.lstrip().startswith("</") or notice and not notice[2])
            kind = tag[1] if tag else "admonition"
            if closing:
                found = next((s for s in reversed(active) if s["kind"] == kind), None)
                if found:
                    for scope in active[active.index(found):]:
                        scope["end"] = item["end"]
                        scope["closed"] = True
                    active = active[:active.index(found)]
                    if kind in ("Tabs", "TabItem"):
                        headings = found["heading_stack"][:]
                        # Tab 内的标题不能泄漏到下一选项或 Tabs 后的普通正文。
                        current["end"] = item["end"]
                        current = {"start": item["end"], "path": [title] + [s["label"] for s in headings
                                                                                 if s["label"] != title],
                                   "level": (headings[-1]["level"] + 1) if headings else 1,
                                   "parent_id": headings[-1]["section_id"] if headings else f"{doc_id}#root",
                                   "section_id": f"{doc_id}#continuation-{item['end']}", "label": "",
                                   "scope_ids": [s["scope_id"] for s in active]}
                        sections.append(current)
            else:
                attrs = dict(re.findall(r"(label|value|groupId)\s*=\s*\{?[\"']([^\"']+)[\"']", line))
                scope = {"scope_id": f"{doc_id}#scope-{item['start']}", "kind": kind,
                         "label": attrs.get("label", attrs.get("value", "")) if tag else (notice[2] or "note"),
                         "start": item["start"], "end": len(text), "heading_stack": headings[:],
                         "closed": bool(tag and line.rstrip().endswith("/>")),
                         "section_id": current["section_id"], "parent_scope_ids": [s["scope_id"] for s in active]}
                scopes.append(scope)
                if not tag or not line.rstrip().endswith("/>"):
                    active.append(scope)
            continue
        if heading:
            current["end"] = item["start"]
            level, label = len(heading[1]), heading[2]
            while headings and headings[-1]["level"] >= level:
                headings.pop()
            current = {"start": item["start"], "level": level,
                       "path": [title] + [s["label"] for s in headings if s["label"] != title] + ([label] if label != title else []),
                       "label": label, "parent_id": headings[-1]["section_id"] if headings else f"{doc_id}#root",
                       "section_id": f"{doc_id}#section-{item['start']}",
                       "scope_ids": [s["scope_id"] for s in active]}
            headings.append(current)
            sections.append(current)
    current["end"] = len(text)
    # subtree_end 用于回取父节，直接正文跨度 end 则仍保持互不重叠。
    for number, section in enumerate(sections):
        section["end"] = section.get("end", len(text))
        section["subtree_end"] = next((s["start"] for s in sections[number + 1:]
                                       if s["level"] <= section["level"]), len(text))
        for scope in scopes:
            if scope["scope_id"] in section["scope_ids"]:
                section["subtree_end"] = min(section["subtree_end"], scope["end"])
    for scope in scopes:
        scope.pop("heading_stack", None)
        scope["text"] = text[scope["start"]:scope["end"]]
    return sections, scopes
