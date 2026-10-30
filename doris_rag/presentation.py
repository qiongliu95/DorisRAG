"""答案与证据的安全 Markdown 展示；不改写证据正文。"""
from __future__ import annotations

import copy
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from .sessions import RETRY_STATUSES


def failure_message(error_type='', http_status=None):
    if http_status == 429:
        return '生成服务当前繁忙，请稍后重试。'
    if http_status in (401,403):
        return '生成服务暂未允许访问，请联系服务维护者。'
    if http_status and http_status >= 500:
        return '生成服务暂时不可用，请稍后重试。'
    if error_type in ('ReadTimeout','Timeout','TimeoutError'):
        return '生成服务响应超时，本次没有完成回答。请稍后重试。'
    if error_type == 'ConnectTimeout':
        return '连接生成服务超时，请稍后重试。'
    if error_type in ('ConnectionError','ProxyError'):
        return '暂时无法连接生成服务，请稍后重试。'
    if error_type == 'SSLError':
        return '与生成服务的安全连接未能建立，请联系服务维护者。'
    return '本次处理未完成，可以重试；如果持续失败，请联系服务维护者。'


def excerpt(text):
    tokens = MarkdownIt('commonmark', {'html':False}).parse(text)
    for n, token in enumerate(tokens[:-1]):
        if token.type == 'paragraph_open' and tokens[n+1].type == 'inline':
            value = ''.join(t.content if t.type in ('text','code_inline') else ' ' if t.type in ('softbreak','hardbreak') else ''
                            for t in tokens[n+1].children or []).strip()
            if value:
                return value[:180] + ('…' if len(value)>180 else '')
    return ''


def markdown_html(text: str, source_url: str = '') -> str:
    parser = MarkdownIt('commonmark', {'html': False}).enable('table').enable('strikethrough')
    # 资料里的 HTML 只作文字；图片不触发外部请求。链接仅允许网页与原文锚点。
    parser.disable('image')
    original = parser.renderer.rules.get('link_open')

    def link_open(tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrGet('href') or ''
        target = urljoin(source_url, href) if source_url else href
        parts = urlsplit(target)
        if source_url and re.search(r'\.mdx?$', parts.path):
            target = urlunsplit(parts._replace(path=re.sub(r'\.mdx?$', '', parts.path)))
        if urlsplit(target).scheme in ('http', 'https') or target.startswith('#'):
            token.attrSet('href', target)
            token.attrSet('rel', 'noopener noreferrer')
            token.attrSet('target', '_blank')
        else:
            token.attrs.pop('href', None)
        return original(tokens, idx, options, env) if original else parser.renderer.renderToken(tokens, idx, options, env)

    parser.renderer.rules['link_open'] = link_open
    return parser.render(text)


def evidence_groups(rows: list[dict]) -> list[dict]:
    groups, by_text, covered, context_by_text = [], {}, {}, {}
    # 先保留所有命中原文跨度，再只展示补取中尚未出现的行；不按文件去重有效片段。
    for row in rows:
        if row.get('source_start_line') and row.get('source_end_line'):
            covered.setdefault(row['doc_id'], set()).update(range(row['source_start_line'], row['source_end_line'] + 1))
    for row in rows:
        key = row['chunk_text'].rstrip()
        if key not in by_text:
            group = {'ranks': [], 'sources': [], 'html': markdown_html(row['chunk_text'], row.get('source_url', '')),
                     'contexts': [], 'partial': False, 'excerpt': excerpt(row['chunk_text'])}
            by_text[key] = group
            groups.append(group)
        group = by_text[key]
        group['ranks'].append(row['rank'])
        group['sources'].append({k: row.get(k) for k in ('rank', 'title', 'source_version', 'source_url',
            'doc_id', 'relative_path', 'section_path', 'source_start_line', 'source_end_line',
            'document_version_notices', 'version_note')})
        group['partial'] |= row.get('context_status') == 'partial'
        seen = covered.setdefault(row['doc_id'], set())
        for context in row.get('contexts', []):
            # 原始行号对应原文文本；解析表头已在片段中显示，不重复附加。
            if context.get('origin') == 'parsed_table_header':
                continue
            lines = context['text'].splitlines(keepends=True)
            start = context['source_start_line']
            part, first = [], None

            def flush():
                if part:
                    source = {'rank': row['rank'], 'doc_id': row['doc_id'],
                        'source_version': row['source_version'], 'start': first, 'end': first + len(part) - 1,
                        'html': markdown_html(''.join(part), row.get('source_url', ''))}
                    text_key = ''.join(part).rstrip()
                    if text_key in context_by_text:
                        context_by_text[text_key]['sources'].append(source)
                    else:
                        item = {**source, 'sources': [source]}
                        group['contexts'].append(item)
                        context_by_text[text_key] = item

            for offset, line in enumerate(lines):
                number = start + offset
                if number in seen:
                    flush()
                    part, first = [], None
                else:
                    if first is None:
                        first = number
                    part.append(line)
                    seen.add(number)
            flush()
    return groups


def present_result(result: dict) -> dict:
    display = result.get('answer', '')
    status = result.get('status')
    if status in ('execution_failed', 'understanding_failed'):
        failure = result.get('diagnostics', {}).get('failure') or {}
        reason = result.get('error_type') or failure.get('error_type')
        display = failure_message(reason or '', result.get('http_status')) if reason else (
            '暂未能可靠理解这份材料，未继续生成答案。可以重试或补充说明。' if status=='understanding_failed' else failure_message())
    elif status in ('llm_not_configured','understanding_unavailable'):
        display = '生成服务尚未配置，暂时无法回答。请联系服务维护者。'
    elif status == 'index_not_ready':
        display = '知识库暂未准备好，请稍后重试。'
    return {**result, 'display_answer':display, 'answer_html': markdown_html(display),
            'evidence_groups': evidence_groups(result.get('evidence', []))}


def present_session(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    for message in doc['messages']:
        if message.get('result'):
            message['result'] = present_result(message['result'])
            if message.get('status') in RETRY_STATUSES:
                message['content'] = message['result']['display_answer']
        message['retry_available'] = (message is doc['messages'][-1] and message.get('status') in RETRY_STATUSES
            and doc.get('retry_message_id')==message.get('id') and bool(doc.get('retry_turn')))
    doc.pop('retry_turn', None)
    doc.pop('retry_message_id', None)
    return doc
