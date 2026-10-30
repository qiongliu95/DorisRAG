from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def config() -> dict:
    value = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    for key in ("source_root", "data_dir"):
        value[key] = str((ROOT / value[key]).resolve())
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def embedding_text(row: dict) -> str:
    tabs = " / ".join(row.get("tab_labels", []))
    return (
        f"Title: {row['title']}\nSection: {row['section_path']}\n"
        f"Source directory version: {row['source_version']}\n"
        + (f"Tab: {tabs}\n" if tabs else "") + f"\n{row['chunk_text']}"
    ).strip()


def tokenizer():
    # SentenceTransformer 的 tokenizer 初始化可能与单独 AutoTokenizer 不同。
    # 切分、校验和查询均使用实际编码模型的 tokenizer，不能只凭相同模型名推断一致。
    return model().tokenizer


def model():
    import torch
    from sentence_transformers import SentenceTransformer
    cfg = config()
    torch.set_num_threads(cfg["torch_threads"])
    location = str(ROOT / cfg["embedding_model_path"]) if cfg.get("embedding_model_path") else cfg["embedding_model"]
    return SentenceTransformer(location, device="cpu", local_files_only=cfg["local_files_only"])


def tokens(tok, text: str) -> int:
    return len(tok.encode(text, add_special_tokens=True, truncation=False, verbose=False))


def model_fingerprint(encoder) -> str:
    # 对实际权重与 tokenizer 做指纹，避免同维度、不同模型误配。
    digest = hashlib.sha256()
    for name, tensor in encoder.state_dict().items():
        digest.update(name.encode())
        digest.update(tensor.detach().cpu().numpy().tobytes())
    digest.update(encoder.tokenizer.backend_tokenizer.to_str().encode())
    return digest.hexdigest()
