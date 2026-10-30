import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from doris_rag import web
from doris_rag.common import write_json


class QueryHTTPTests(unittest.TestCase):
    def test_pointer_alone_does_not_advertise_verified_index(self):
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.web.ROOT',Path(temp)), patch('doris_rag.web.config',return_value={'data_dir':temp}):
            write_json(Path(temp)/'index/current.json',{'build_id':'fixture'})
            write_json(Path(temp)/'reports/round1_20261023/run_status.json',{'phase':'checking'})
            write_json(Path(temp)/'reports/round1_20261023/build_checks.json',{'passed':True,'build_id':'fixture'})
            self.assertFalse(web.index_ready())
            write_json(Path(temp)/'reports/round1_20261023/run_status.json',{'phase':'completed','build_id':'fixture'})
            self.assertTrue(web.index_ready())

    def test_status_warms_only_once_and_records_cost(self):
        from concurrent.futures import Future
        done=Future();done.set_result({'build_id':'fixture','seconds':2.5})
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.web.config',return_value={'data_dir':temp,'retrieval_mode':'hybrid_precise'}),patch(
                'doris_rag.web.index_ready',return_value=True),patch('doris_rag.web.warm_future',None),patch(
                'doris_rag.web.workers.submit',return_value=done) as submit:
            write_json(Path(temp)/'index/current.json',{'build_id':'fixture'})
            first=web.status();second=web.status()
        self.assertTrue(first['retrieval_ready']);self.assertEqual(second['warmup']['seconds'],2.5)
        self.assertEqual(submit.call_count,1)

    def test_unfinished_index_returns_503_without_model_or_llm(self):
        with tempfile.TemporaryDirectory() as temp, patch("doris_rag.web.config", return_value={
                "data_dir": temp, "top_k": 5, "retrieval_mode": "hybrid_precise"}), patch("doris_rag.web.run_query") as query:
            response = TestClient(web.app).post("/api/ask", json={"query": "PI()"})
            self.assertEqual(response.status_code, 503)
            query.assert_not_called()

    def test_http_passes_auto_version_and_evidence_flag_to_shared_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            write_json(Path(temp) / "index/current.json", {"build_id": "fixture"})
            retrieval = Mock(manifest={"build_id": "fixture"})
            with patch("doris_rag.web.index_ready", return_value=True), patch("doris_rag.web.retriever", retrieval), patch("doris_rag.web.config", return_value={
                    "data_dir": temp, "top_k": 5, "retrieval_mode": "hybrid_precise"}), patch(
                    "doris_rag.web.run_query", return_value={"status": "evidence_only", "evidence": []}) as query:
                response = TestClient(web.app).post("/api/ask", json={"query": "Doris3.x PI()", "evidence_only": True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(query.call_args.args, ("Doris3.x PI()", None, 5, "hybrid_precise", True, retrieval))

    def test_long_material_accepted_but_empty_material_rejected(self):
        from doris_rag.web import Question
        self.assertEqual(len(Question(query="x" * 6000).query), 6000)
        self.assertEqual(TestClient(web.app).post("/api/ask", json={"query": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
