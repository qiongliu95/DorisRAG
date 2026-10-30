"""构建后验收：当前真实向量/索引与原始快照，报告不代表回答质量。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import faiss
import numpy as np

from doris_rag.chunking import source_context
from doris_rag.common import ROOT, config, read_jsonl, sha256_file, write_json
from doris_rag.retrieval import Retriever


def check():
    cfg = config()
    data = Path(cfg["data_dir"])
    report = ROOT / "reports/round1_20261023"
    pointer = json.loads((data / "index/current.json").read_text(encoding="utf-8"))
    directory = data / "builds" / pointer["build_id"]
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    vectors = np.load(directory / "vectors.npy", mmap_mode="r")
    index = faiss.read_index(str(directory / "faiss.index"))
    rows = json.loads((directory / "chunk_metadata.json").read_text(encoding="utf-8"))
    expected = read_jsonl(data / "processed/chunks.jsonl")
    expected.sort(key=lambda row: (-row["embedding_tokens"], row["chunk_id"]))
    errors = []
    if vectors.shape != (len(rows), 768) or index.ntotal != len(rows) or index.d != 768:
        errors.append("count_or_dimension_mismatch")
    for start in range(0, len(rows), 1024):
        values = vectors[start:start + 1024]
        if not np.isfinite(values).all() or not np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-5):
            errors.append(f"invalid_vectors_at_{start}")
        if not np.array_equal(values, index.reconstruct_n(start, len(values))):
            errors.append(f"index_vector_order_mismatch_at_{start}")
    if expected != rows:
        errors.append("metadata_source_or_order_mismatch")
    del expected
    survey = {row["path"]: row["sha256"] for row in read_jsonl(ROOT / "reports/read_only_survey_20261023/source_manifest.jsonl")}
    docs = read_jsonl(data / "processed/documents.jsonl")
    for doc in docs:
        if survey.get(doc["repo_path"]) != doc["source_sha256"] or sha256_file(Path(doc["original_path"])) != doc["source_sha256"]:
            errors.append("source_changed:" + doc["doc_id"])
    weight = ROOT / cfg["embedding_model_path"] / "pytorch_model.bin"
    expected_weight_hash = (weight.parent / ".cache/huggingface/download/pytorch_model.bin.metadata").read_text(encoding="utf-8").splitlines()[1]
    if sha256_file(weight) != expected_weight_hash:
        errors.append("downloaded_weight_hash_mismatch")
    # 从同一 metadata 实际建 BM25 并执行检索，验证新索引可供既有流程读取。
    retriever = Retriever()
    searches = []
    for version in ("4.x", "3.x", "2.1"):
        evidence = retriever.search("PI 函数返回什么", version, 3, cfg["retrieval_mode"])
        valid = bool(evidence) and all(r["source_version"] == version for r in evidence)
        if not valid:
            errors.append("search_smoke_failed:" + version)
        searches.append({"version": version, "chunk_ids": [r["chunk_id"] for r in evidence], "passed": valid})
    context_checks = []
    for label, predicate in (("code", lambda r: r["has_code_block"]),
                             ("table", lambda r: "table" in r["content_types"]),
                             ("tab", lambda r: bool(r["tab_labels"]))):
        row = next(r for r in rows if predicate(r))
        valid = all(bool(source_context(row, mode)) for mode in ("source", "related", "section"))
        if not valid:
            errors.append("context_smoke_failed:" + label)
        context_checks.append({"kind": label, "chunk_id": row["chunk_id"], "passed": valid})
    result = {"build_id": pointer["build_id"], "model": manifest["embedding_model"],
              "vectors_shape": list(vectors.shape), "vectors_checked": len(rows),
              "faiss_type": type(index).__name__, "normalized": True,
              "all_metadata_rows_equal_source": "metadata_source_or_order_mismatch" not in errors,
              "source_files_checked_against_original_survey": len(docs),
              "weight_sha256": sha256_file(weight),
              "search_smoke": searches, "context_smoke": context_checks,
              "errors": errors, "passed": not errors,
              "scope": "index/data integrity and existing retrieval smoke only; no answer-quality claim"}
    write_json(report / "build_checks.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    check()
