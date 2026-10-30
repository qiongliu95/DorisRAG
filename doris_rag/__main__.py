from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import config, tokenizer


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Doris 文档入库、检索、问答与评测")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("build")
    commands.add_parser("verify")
    for command in ("search", "ask"):
        item = commands.add_parser(command)
        item.add_argument("query")
        item.add_argument("--version", choices=["auto", "4.x", "3.x", "2.1", "all"], default=None)
        item.add_argument("--top-k", type=int, default=config()["top_k"])
        item.add_argument("--mode", default=config()["retrieval_mode"])
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--mode", default=config()["retrieval_mode"])
    evaluation.add_argument("--answers", action="store_true")
    evaluation.add_argument("--cases", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        from .documents import import_documents
        from .chunking import prepare_chunks
        docs, audit = import_documents()
        print(json.dumps(audit, ensure_ascii=False, indent=2), flush=True)
        print(json.dumps(prepare_chunks(docs, tokenizer()), ensure_ascii=False, indent=2))
    elif args.command == "verify":
        from .audit import verify
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
    elif args.command == "build":
        from .indexing import build_index
        print(json.dumps(build_index(), ensure_ascii=False, indent=2))
    elif args.command in ("search", "ask"):
        from .pipeline import run_query
        result = run_query(args.query, args.version, args.top_k, args.mode, evidence_only=args.command == "search")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        import uvicorn
        from .runtime_logging import server_log_config
        uvicorn.run("doris_rag.web:app", host="127.0.0.1", port=args.port, log_config=server_log_config())
    else:
        from .evaluation import evaluate
        print(json.dumps(evaluate(args.mode, args.answers, args.cases), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
