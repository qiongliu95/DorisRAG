from __future__ import annotations

import json
import time
from pathlib import Path

import faiss
import numpy as np

from .common import (ROOT, config, embedding_text, model, model_fingerprint, read_jsonl,
                     sha256_file, tokens, write_json)


def build_index() -> dict:
    cfg = config()
    data = Path(cfg["data_dir"])
    chunks_path = data / "processed" / "chunks.jsonl"
    chunks_hash, config_hash = sha256_file(chunks_path), sha256_file(ROOT / "config.json")
    verified = json.loads((data / "manifests" / "verification.json").read_text(encoding="utf-8"))
    if (not verified["passed"] or verified.get("chunks_sha256") != chunks_hash
            or verified.get("documents_sha256") != sha256_file(data / "processed" / "documents.jsonl")
            or verified.get("config_sha256") != config_hash):
        raise ValueError("必须先通过当前数据与配置的解析完整性检查，才能编码向量")
    rows = read_jsonl(chunks_path)
    if not rows:
        raise ValueError("没有可索引的 chunk")
    # 同长度输入成批编码，减少 CPU padding；metadata 始终与此顺序一致。
    rows.sort(key=lambda row: (-row["embedding_tokens"], row["chunk_id"]))
    if len({r["chunk_id"] for r in rows}) != len(rows):
        raise ValueError("chunk_id 不唯一")
    encoder = model()
    fingerprint = model_fingerprint(encoder)
    texts = [embedding_text(row) for row in rows]
    measured = [tokens(encoder.tokenizer, text) for text in texts]
    limit = min(cfg["max_embedding_tokens"], encoder.max_seq_length)
    if any(n > limit for n in measured):
        raise ValueError("存在超长 embedding 输入，拒绝静默截断")
    build_id = f"{chunks_hash[:12]}-{config_hash[:10]}"
    directory = data / "builds" / build_id
    directory.mkdir(parents=True, exist_ok=True)
    progress_path = directory / "progress.json"
    dimension = encoder.get_sentence_embedding_dimension()
    if dimension != cfg["embedding_dimension"]:
        raise ValueError("模型原生维度与基线不符，拒绝构建")
    identity = {"chunks_sha256": chunks_hash, "config_sha256": config_hash,
                "model_fingerprint": fingerprint, "num_chunks": len(rows), "dimension": dimension}
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        completed = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(completed[key] != value for key, value in identity.items()):
            raise ValueError("已完成构建身份冲突，拒绝覆盖")
        for filename, key in (("faiss.index", "index_sha256"), ("chunk_metadata.json", "metadata_sha256"),
                              ("vectors.npy", "vectors_sha256")):
            if sha256_file(directory / filename) != completed[key]:
                raise ValueError("已完成构建损坏，拒绝覆盖")
        active = data / "index" / "current.json"
        write_json(active.with_suffix(".tmp"), {"build_id": build_id})
        active.with_suffix(".tmp").replace(active)
        return completed
    done = 0
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress["identity"] != identity:
            raise ValueError("断点与数据/模型不一致，拒绝复用")
        done = progress["done"]
    vectors = np.lib.format.open_memmap(directory / "vectors.npy", mode="r+" if done else "w+",
                                        dtype=np.float32, shape=(len(rows), dimension))
    started = time.time()
    for start in range(done, len(rows), cfg["batch_size"]):
        end = min(start + cfg["batch_size"], len(rows))
        values = encoder.encode(texts[start:end], batch_size=cfg["batch_size"],
                                convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
        if not np.isfinite(values).all() or not np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-5):
            raise ValueError("embedding 出现 NaN/Inf 或未正确归一化")
        vectors[start:end] = values
        vectors.flush()
        # 先持久化向量，再提交断点；中断最多重算当前批次。
        write_json(progress_path.with_suffix(".tmp"), {"identity": identity, "done": end})
        progress_path.with_suffix(".tmp").replace(progress_path)
        if start == done or end % (cfg["batch_size"] * 10) == 0 or end == len(rows):
            print(f"Embedding {end}/{len(rows)}, elapsed={time.time()-started:.1f}s", flush=True)
    index = faiss.IndexFlatIP(dimension)
    index.add(np.asarray(vectors))
    faiss.write_index(index, str(directory / "faiss.index"))
    write_json(directory / "chunk_metadata.json", rows)
    manifest = {**identity, "build_id": build_id, "embedding_model": cfg["embedding_model"],
                "query_prefix": cfg["query_prefix"], "normalized": True, "faiss_type": "IndexFlatIP",
                "max_embedding_tokens": max(measured), "token_limit": limit, "truncated_inputs": 0,
                "index_sha256": sha256_file(directory / "faiss.index"),
                "metadata_sha256": sha256_file(directory / "chunk_metadata.json"),
                "vectors_sha256": sha256_file(directory / "vectors.npy"),
                "documents_sha256": verified["documents_sha256"],
                "verification_sha256": sha256_file(data / "manifests" / "verification.json"),
                "source_snapshot_sha256": json.loads((data / "manifests" / "import_audit.json").read_text(encoding="utf-8"))["source_snapshot_sha256"]}
    saved_index = faiss.read_index(str(directory / "faiss.index"))
    if saved_index.ntotal != len(rows) or saved_index.d != dimension:
        raise ValueError("索引向量数量或维度错误")
    for start in range(0, len(rows), 1024):
        saved_vectors = saved_index.reconstruct_n(start, min(1024, len(rows) - start))
        if not np.array_equal(saved_vectors, vectors[start:start + len(saved_vectors)]):
            raise ValueError("FAISS 与向量文件顺序或内容不一致")
    write_json(directory / "config_snapshot.json", cfg)
    write_json(directory / "manifest.json", manifest)
    # 完整构建与校验后才切换活动索引，界面不会读到半成品。
    active = data / "index" / "current.json"
    write_json(active.with_suffix(".tmp"), {"build_id": build_id})
    active.with_suffix(".tmp").replace(active)
    return manifest
