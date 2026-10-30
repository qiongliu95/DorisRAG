"""保留完整参数名的轻量 BM25；用于与向量检索作对照。"""
from __future__ import annotations

import re


def exact_literals(text: str, literals: list[str]) -> list[str]:
    hits = []
    for literal in literals:
        # 只允许完整 ASCII 技术词或完整错误原串，标点和内部空格均不可归一化。
        pattern = (r"(?<![a-z0-9_])" if literal[:1].isascii() and literal[:1].isalnum() else "")
        pattern += re.escape(literal)
        pattern += r"(?![a-z0-9_])" if literal[-1:].isascii() and literal[-1:].isalnum() else ""
        if literal and re.search(pattern, text, re.I):
            hits.append(literal)
    return hits


def lexical_tokens(text: str) -> list[str]:
    text = text.lower()
    words = re.findall(r"[a-z_][a-z_0-9.]*|\d+(?:\.\d+)*", text)
    result = list(words)
    # 完整标识符不能只拆成通用词；同时保留子词支持自然语言提问。
    for word in words:
        if "_" in word:
            result.extend(part for part in word.split("_") if part)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(run[i:i + 2] for i in range(len(run) - 1)) if len(run) > 1 else result.append(run)
    return result


class LexicalIndex:
    def __init__(self, rows: list[dict]):
        # 页面、会话和澄清不需要加载数值计算依赖；首次真实检索时再初始化。
        import numpy as np
        from sklearn.feature_extraction.text import CountVectorizer
        self.vectorizer = CountVectorizer(analyzer=lexical_tokens, lowercase=False, dtype=np.float32)
        texts = [f"{r['title']}\n{r['section_path']}\n{r['chunk_text']}" for r in rows]
        self.texts = texts
        counts = self.vectorizer.fit_transform(texts).tocsr()
        length = np.asarray(counts.sum(axis=1)).ravel()
        frequency = np.diff(counts.tocsc().indptr)
        inverse = np.log1p((len(rows) - frequency + 0.5) / (frequency + 0.5))
        k1, b = 1.5, 0.75
        denominator = k1 * (1 - b + b * length / max(float(length.mean()), 1))
        # CSR 上就地计算，避免为每次查询扫描并切词全部文档。
        counts.data = ((counts.data * (k1 + 1)) /
                       (counts.data + np.repeat(denominator, np.diff(counts.indptr)))) * inverse[counts.indices]
        self.weights = counts

    def scores(self, query: str) -> np.ndarray:
        query_vector = self.vectorizer.transform([query])
        query_vector.data[:] = 1
        return (self.weights @ query_vector.T).toarray().ravel()

    def ranked(self, query: str, eligible: list[int], limit: int) -> list[tuple[int, float]]:
        import numpy as np
        scores = self.scores(query)
        eligible_array = np.asarray(eligible, dtype=np.int64)
        values = scores[eligible_array]
        order = np.argsort(-values, kind="stable")[:limit]
        return [(int(eligible_array[i]), float(values[i])) for i in order if values[i] > 0]

    def exact_ranked(self, literals: list[str], eligible: list[int], limit: int) -> list[tuple[int, float]]:
        if not literals:
            return []
        hits = [(i, len(exact_literals(self.texts[i], literals))) for i in eligible]
        return [(i, float(n)) for i, n in sorted(hits, key=lambda item: (-item[1], item[0])) if n][:limit]
