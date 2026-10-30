import unittest
import tempfile
from pathlib import Path

from doris_rag.chunking import sections, split_document
from doris_rag.common import config
from doris_rag.documents import DOCS_PATH, load_document, version_statements
from doris_rag.html_tables import table_to_markdown
from doris_rag.markdown import clean_with_mapping, structure
from test_pipeline import CharacterTokenizer, document


class BaselineProcessingTests(unittest.TestCase):
    def split(self, text, budget=300):
        parsed = clean_with_mapping(text, 8)
        doc = {**document(parsed["cleaned_text"]), **parsed}
        rows = split_document(doc, CharacterTokenizer(), budget)
        self.assertEqual("".join(doc["cleaned_text"][r["start_char"]:r["end_char"]] for r in rows), doc["cleaned_text"])
        self.assertTrue(all(r["embedding_tokens"] <= budget for r in rows))
        return rows

    def test_inline_triple_backticks_do_not_swallow_heading(self):
        text = "## A\n```max_size=1024```\n## B\n后文"
        self.assertEqual(len(sections(text, "测试")), 2)
        self.assertEqual(clean_with_mapping(text)["parse_warnings"], [])

    def test_unclosed_fence_recovers_and_records_source_line(self):
        parsed = clean_with_mapping("## A\n```sql\nSELECT 1;\n## B\n说明", 10)
        self.assertEqual(len(sections(parsed["cleaned_text"], "测试")), 2)
        self.assertEqual(parsed["parse_warnings"][0]["source_start_line"], 11)
        self.assertIn("SELECT 1;", parsed["cleaned_text"])

    def test_unrelated_short_sections_never_merge(self):
        rows = self.split("## ABS 函数\n返回绝对值。\n## 配置用户权限\n授予权限。")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["parent_section_id"], rows[1]["parent_section_id"])

    def test_repeated_headings_have_distinct_parent_nodes(self):
        rows = self.split("## 参数\n甲。\n## 参数\n乙。")
        self.assertNotEqual(rows[0]["parent_section_id"], rows[1]["parent_section_id"])
        self.assertEqual(rows[0]["source_start_line"], 8)
        self.assertEqual(rows[1]["source_start_line"], 10)

    def test_tabs_bind_labels_and_restore_outer_heading(self):
        rows = self.split('## 连接\n<Tabs>\n<TabItem label="Java">\n### 示例\njava_rule\n</TabItem>\n<TabItem label="Go">\ngo_rule\n</TabItem>\n</Tabs>\nouter_rule')
        java = next(r for r in rows if "java_rule" in r["chunk_text"])
        go = next(r for r in rows if "go_rule" in r["chunk_text"])
        outer = next(r for r in rows if "outer_rule" in r["chunk_text"])
        self.assertEqual(java["tab_labels"], ["Java"])
        self.assertEqual(go["tab_labels"], ["Go"])
        self.assertEqual(outer["tab_labels"], [])
        self.assertNotIn("示例", go["section_path"])
        self.assertNotIn("示例", outer["section_path"])

    def test_midpage_admonition_binds_descendants_not_sibling(self):
        text = "## A\n" + "\n" * 60 + ":::caution\n必须先开启选项。\n自3.0.2起支持。\n:::\n### 规则\nrule_a\n## B\nrule_b"
        rows = self.split(text)
        a = next(r for r in rows if "rule_a" in r["chunk_text"])
        b = next(r for r in rows if "rule_b" in r["chunk_text"])
        self.assertTrue(any("必须先开启" in c["text"] for c in a["applicability_conditions"]))
        self.assertFalse(b["applicability_conditions"])
        self.assertGreater(a["applicability_conditions"][0]["source_start_line"], 50)

    def test_tab_condition_does_not_leak_to_sibling_option(self):
        rows = self.split('<Tabs>\n<TabItem label="Java">\n:::tip\n仅客户端 Java 生效\n:::\njava_rule\n</TabItem>\n<TabItem label="Go">\ngo_rule\n</TabItem>\n</Tabs>')
        go = next(r for r in rows if "go_rule" in r["chunk_text"])
        self.assertFalse(go["applicability_conditions"])

    def test_version_chinese_boundaries_and_decimal(self):
        self.assertEqual(version_statements("自3.0.2起支持该参数")[0]["versions"], ["3.0.2"])
        self.assertFalse(version_statements("Pi=3.1415926535 默认"))

    def test_multilevel_table_header_survives_conversion_and_split(self):
        html = '<table><thead><tr><th colspan="2">限制</th></tr><tr><th>名称</th><th>默认值</th></tr></thead><tbody>' + ''.join(f'<tr><td>option_{i}</td><td>{i}</td></tr>' for i in range(40)) + '</tbody></table>'
        converted = table_to_markdown(html)
        self.assertIn("限制 / 名称", converted)
        rows = self.split(converted)
        for row in rows:
            if row["split_kind"] == "oversize_table":
                self.assertIn("限制 / 默认值", row["table_header"])
                self.assertTrue(row["requires_parent_context"])

    def test_sql_explanation_and_result_have_resolvable_links(self):
        rows = self.split("## 示例\n说明输入与结果。\n\n```sql\nSELECT 1;\n```\n\n结果如下：\n\n| value |\n| --- |\n| 1 |", 180)
        refs = {b["block_id"] for r in rows for b in r["block_refs"]}
        self.assertTrue(any("code" in r["content_types"] for r in rows))
        self.assertTrue(all(set(r["related_block_ids"]) <= refs for r in rows))

    def test_real_be_config_keeps_post_inline_headings(self):
        cfg = config()
        path = Path(cfg["source_root"]) / DOCS_PATH / "version-4.x/admin-manual/config/be-config.md"
        doc = load_document(path, "4.x", Path(cfg["source_root"]))
        self.assertGreater(len(doc["sections"]), 70)
        self.assertTrue(all(s["source_start_line"] >= 1 for s in doc["sections"]))

    def test_raw_version_locations_and_tab_scope_are_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / DOCS_PATH / "version-3.x/test.mdx"
            path.parent.mkdir(parents=True)
            path.write_text('---\ntitle: Test\n---\n\n## 条件\n<Tabs>\n<TabItem label="Java">\n:::tip\n自3.0.2起支持\n:::\njava_rule\n</TabItem>\n<TabItem label="Go">\ngo_rule\n</TabItem>\n</Tabs>', encoding="utf-8")
            doc = load_document(path, "3.x", root)
            self.assertEqual(doc["version_statements"][0]["line"], 9)
            rows = split_document(doc, CharacterTokenizer(), 300)
            java = next(r for r in rows if "java_rule" in r["chunk_text"])
            go = next(r for r in rows if "go_rule" in r["chunk_text"])
            self.assertTrue(java["version_statements"])
            self.assertFalse(go["version_statements"])
            self.assertFalse(doc["document_version_notices"])

    def test_code_markdown_headings_and_comment_literals_survive(self):
        text = "## 例子\n```markdown\n## 这是代码\n<!--literal-->\n```\n## 真标题\n正文"
        self.assertEqual(len(sections(text, "测试")), 2)
        self.assertEqual(clean_with_mapping(text)["cleaned_text"], text)


if __name__ == "__main__":
    unittest.main()
