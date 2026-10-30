import unittest
from pathlib import Path

from doris_rag.chunking import sections, split_document
from doris_rag.common import config, read_jsonl, sha256_file
from doris_rag.documents import clean_markdown, front_matter, version_statements
from doris_rag.generation import messages
from doris_rag.generation import answer
from doris_rag.html_tables import table_to_markdown


class CharacterTokenizer:
    def encode(self, text, **kwargs):
        return list(range(len(text) + 2))


def document(text):
    return {"doc_id": "fixture", "source": "test", "source_version": "3.x", "locale": "zh-CN",
            "title": "测试", "topic": "test", "topic_path": "test", "relative_path": "test.md",
            "repo_path": "test.md", "original_path": "test.md", "source_url": "https://example.invalid/test",
            "source_sha256": "test", "cleaned_sha256": "test", "cleaned_text": text,
            "document_version_notices": [], "applicability_status": "not_explicitly_stated", "quality_warnings": []}


class ProcessingTests(unittest.TestCase):
    def assert_complete(self, text, budget=240):
        rows = split_document(document(text), CharacterTokenizer(), budget)
        self.assertTrue(rows)
        self.assertEqual("".join(text[r["start_char"]:r["end_char"]] for r in rows), text)
        self.assertTrue(all(r["embedding_tokens"] <= budget for r in rows))
        return rows

    def test_short_chinese_function_survives(self):
        self.assert_complete("## 描述\n返回常量。\n## 语法\n```sql\nPI()\n```\n")

    def test_fence_comments_are_not_headings(self):
        text = "## 设置\n```bash\n# shell comment\necho ok\n```\n## 结束\n正文"
        self.assertEqual(len(sections(text, "测试")), 2)
        self.assert_complete(text)

    def test_tilde_and_longer_fences(self):
        text = "## 标题\n~~~~text\n# not heading\n```\n~~~~\n"
        self.assertEqual(len(sections(text, "测试")), 1)
        self.assert_complete(text)

    def test_long_code_has_full_spans_and_bounded_inputs(self):
        rows = self.assert_complete("## SQL\n```sql\n" + "select '中文值', 123;\n" * 60 + "```\n")
        self.assertTrue(any(r["split_kind"] == "oversize_code" for r in rows))
        self.assertTrue(all(r["chunk_text"].count("```") % 2 == 0 for r in rows))

    def test_long_table_keeps_all_parameter_values(self):
        text = "## 参数\n| 参数 | 说明 |\n| --- | --- |\n" + "".join(f"| option_{i} | 值{i} |\n" for i in range(60))
        rows = self.assert_complete(text)
        self.assertTrue(any(r["split_kind"] == "oversize_table" for r in rows))

    def test_later_unclosed_fence_does_not_break_closed_fragments(self):
        doc = document("```sql\n" + "SELECT 123;\n" * 50 + "```\n\n## 后文\n```text\n原文未闭合")
        doc["quality_warnings"] = ["unclosed_source_fence"]
        rows = split_document(doc, CharacterTokenizer(), 240)
        for row in rows:
            if row["split_kind"] == "oversize_code":
                self.assertEqual(row["chunk_text"].count("```") % 2, 0)

    def test_mdx_preserves_labels_sql_placeholders_and_warnings(self):
        source = "import Tabs from '@theme/Tabs';\n<TabItem label=\"Java\">\nArray<T>\n```sql\nSELECT <column>;\n```\n</TabItem>\n:::caution 4.1.4\n接口已移除\n:::"
        clean, _ = clean_markdown(source)
        self.assertIn("Java", clean)
        self.assertIn("Array<T>", clean)
        self.assertIn("SELECT <column>;", clean)
        self.assertIn("接口已移除", clean)
        self.assertNotIn("import Tabs", clean)

    def test_inline_api_placeholders_are_not_html(self):
        source = "请求：`POST /api/<db>/<table>/_query_plan`，输入 `<table>$<system_table_name>`。"
        clean, _ = clean_markdown(source)
        self.assertEqual(source, clean)

    def test_frontmatter_and_horizontal_rule(self):
        meta, body = front_matter('---\n{"title":"标题", "slug":"/abc"}\n---\n正文\n---\n保留')
        self.assertEqual(meta["slug"], "/abc")
        self.assertIn("\n---\n", body)

    def test_html_table_retains_header_values_and_rowspan(self):
        text = table_to_markdown('<table><tr><th>参数</th><th>值</th></tr><tr><td rowspan="2">limit</td><td>1024</td></tr><tr><td>256</td></tr></table>')
        self.assertIn("| 参数 | 值 |", text)
        self.assertIn("| limit | 1024 |", text)
        self.assertIn("| limit | 256 |", text)

    def test_applicability_quotes_not_inferred_from_directory(self):
        result = version_statements("该参数自 3.1.0 版本支持。\nPi=3.14159")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["versions"], ["3.1.0"])

    def test_global_version_notice_reaches_answer_context(self):
        row = {**document(""), "rank": 1, "section_paths": ["Request"], "chunk_text": "POST /api/upload",
               "split_kind": "structure", "document_version_notices": ["4.1.4 起已移除"]}
        prompt = messages("如何上传？", [row], "4.x")[-1]["content"]
        self.assertIn("4.1.4 起已移除", prompt)
        self.assertIn("不代表整篇适用于此版本", prompt)

    def test_generation_transport_and_citation_validation(self):
        from unittest.mock import Mock, patch
        row = {**document(""), "rank": 1, "section_paths": ["章节"], "chunk_text": "PI()",
               "split_kind": "structure"}
        cfg = {"api_key": "test-not-a-secret", "base_url": "https://example.invalid/v1", "model": "test"}
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason":"stop","message": {"content": "返回 Pi。[证据 1]"}}]}
        with patch("doris_rag.generation.llm_config", return_value=cfg), patch("doris_rag.generation.requests.post", return_value=response) as post:
            result = answer("PI()？", [row], "3.x")
            self.assertTrue(result["citations_valid"])
            self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0.1)
            response.json.return_value = {"choices": [{"finish_reason":"stop","message": {"content": "错误编号 [证据 8]"}}]}
            self.assertFalse(answer("PI()？", [row], "3.x")["citations_valid"])

    def test_missing_llm_is_not_a_generated_answer(self):
        from unittest.mock import patch
        with patch("doris_rag.generation.llm_config", return_value={"api_key": "", "base_url": "", "model": ""}):
            self.assertEqual(answer("问题", [], "4.x")["status"], "llm_not_configured")

    def test_lexical_preserves_full_identifier_and_version(self):
        from doris_rag.lexical import lexical_tokens
        terms = lexical_tokens("max_routine_load_task_num_per_be 自3.1.0支持")
        self.assertIn("max_routine_load_task_num_per_be", terms)
        self.assertIn("3.1.0", terms)
        self.assertIn("支持", terms)

    def test_comparison_source_families_do_not_parse_pi_as_version(self):
        from doris_rag.retrieval import comparison_versions
        self.assertEqual(comparison_versions("Doris 2.1.4 与 3.x 的差别"), ["2.1", "3.x"])
        self.assertEqual(comparison_versions("Doris2.1.4和3.x的差别"), ["2.1", "3.x"])
        self.assertEqual(comparison_versions("PI() 返回 3.141592653589793"), [])

    def test_precise_identifier_is_not_a_substring_match(self):
        from doris_rag.retrieval import identifier_matches
        row = {"title": "参数", "section_path": "参数", "chunk_text": "enable_insert_strict_mode"}
        self.assertEqual(identifier_matches("enable_insert_strict", row), 0)
        row["chunk_text"] = "`enable_insert_strict` = false"
        self.assertEqual(identifier_matches("enable_insert_strict", row), 1)


class ImportedDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Path(config()["data_dir"])
        if not (cls.data / "processed" / "chunks.jsonl").exists():
            raise unittest.SkipTest("尚未完成 prepare")
        cls.docs = read_jsonl(cls.data / "processed" / "documents.jsonl")
        cls.chunks = read_jsonl(cls.data / "processed" / "chunks.jsonl")

    def test_source_files_unchanged(self):
        for doc in self.docs:
            self.assertEqual(sha256_file(Path(doc["original_path"])), doc["source_sha256"])

    def test_counts_and_unique_ids(self):
        from collections import Counter
        self.assertEqual(Counter(d["source_version"] for d in self.docs), {"4.x": 1572, "3.x": 1308, "2.1": 1229})
        self.assertEqual(len({c["chunk_id"] for c in self.chunks}), len(self.chunks))

    def test_every_nonempty_document_and_real_pi_is_present(self):
        ids = {c["doc_id"] for c in self.chunks}
        for doc in self.docs:
            if doc["cleaned_text"]:
                self.assertIn(doc["doc_id"], ids)
        self.assertTrue(any(c["relative_path"].endswith("/pi.md") and c["source_version"] == "4.x" for c in self.chunks))


if __name__ == "__main__":
    unittest.main()
