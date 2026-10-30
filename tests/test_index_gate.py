import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doris_rag.common import ROOT, sha256_file, write_json
from doris_rag.indexing import build_index


class IndexGateTests(unittest.TestCase):
    def test_stale_verification_cannot_start_model_or_vector_build(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "processed").mkdir()
            chunks = data / "processed/chunks.jsonl"
            docs = data / "processed/documents.jsonl"
            chunks.write_text('{"chunk_id":"changed"}\n', encoding="utf-8")
            docs.write_text('{}\n', encoding="utf-8")
            write_json(data / "manifests/verification.json", {
                "passed": True, "chunks_sha256": "previous_snapshot",
                "documents_sha256": sha256_file(docs),
                "config_sha256": sha256_file(ROOT / "config.json"),
            })
            with patch("doris_rag.indexing.config", return_value={"data_dir": str(data)}), patch("doris_rag.indexing.model") as encoder:
                with self.assertRaisesRegex(ValueError, "完整性检查"):
                    build_index()
                encoder.assert_not_called()
            self.assertFalse((data / "builds").exists())


if __name__ == "__main__":
    unittest.main()
