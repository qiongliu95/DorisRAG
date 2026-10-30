import unittest
from unittest.mock import patch, Mock

from doris_rag.sessions import prepare_turn
from doris_rag.query_understanding import understand_query
from doris_rag.generation import answer
from doris_rag.presentation import markdown_html, evidence_groups
from doris_rag.retrieval import substantive
from doris_rag.pipeline import run_query


class DirectedQAFixes(unittest.TestCase):
    def test_scope_only_followups_keep_intent_and_confirmed_conditions(self):
        state = prepare_turn({}, 'Doris 3.x 的负载均衡是什么？')['state']
        for question, expected in [('所有版本都查一下。', ['4.x', '3.x', '2.1']),
                                   ('只查 Doris 2.1。', ['2.1'])]:
            turn = prepare_turn(state, question)
            self.assertEqual(turn['kind'], 'follow_up')
            self.assertIn('负载均衡', turn['query'])
            self.assertEqual(understand_query(turn['query'], turn['version'], version_scope=turn.get('version_scope'))['versions'], expected)
        state = prepare_turn({}, 'Doris 3.x 参数 option_a=false 有什么作用？')['state']
        self.assertIn('option_a=false', prepare_turn(state, '所有版本都查一下。')['query'])

    def test_independent_comparison_and_explicit_switch_clear_old_topic(self):
        state = prepare_turn({}, '什么是负载均衡？')['state']
        for question in ('比较所有版本的 Routine Load 支持情况。', '换个问题，所有版本的 PI() 呢？',
                         'Routine Load 是什么？'):
            turn = prepare_turn(state, question)
            self.assertNotIn('负载均衡', turn['query'])

    def test_empty_structure_is_not_evidence_but_code_and_facts_are(self):
        for text in ('## 常见问题\n\n', ':::note\n', ':::','![拓扑图](image.png)'):
            self.assertFalse(substantive(text))
        for text in ('## 描述\n返回常量。', '```sql\nSELECT PI();\n```', '|参数|值|\n|-|-|\n|a|1|'):
            self.assertTrue(substantive(text))

    def test_natural_answer_keeps_citation_validation_and_no_audit_injection(self):
        evidence = {'rank': 1, 'doc_id': 'fixture', 'title': '连接说明', 'source_version': '4.x',
                    'section_paths': ['连接说明'], 'source_url': 'https://example.invalid',
                    'chunk_text': '连接分发至多个FE', 'split_kind': 'structure'}
        with patch('doris_rag.generation.llm_config', return_value={'model': 'fixture'}), patch(
                'doris_rag.generation.chat_completion', return_value='连接可分发至多个 FE。[证据 1]'):
            result = answer('什么是负载均衡？', [evidence], '4.x', {'facts': [], 'conditions': []})
        self.assertEqual(result['status'], 'generated')
        self.assertNotIn('【用户事实】', result['answer'])
        with patch('doris_rag.generation.llm_config', return_value={'model': 'fixture'}), patch(
                'doris_rag.generation.chat_completion', return_value='错误编号[证据 99]'):
            self.assertEqual(answer('问题', [evidence], '4.x')['status'], 'answer_validation_failed')

    def test_markdown_formats_and_escapes_untrusted_html(self):
        text = '# 标题\n\n- **列表**\n\n```sql\nSELECT 1 < 2;\n```\n\n|a|b|\n|-|-|\n|1|2|\n\n[文档](./other.md#test)\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))'
        rendered = markdown_html(text, 'https://doris.apache.org/zh-CN/docs/4.x/test/page')
        for tag in ('<h1>', '<ul>', '<strong>', '<pre><code', '<table>'):
            self.assertIn(tag, rendered)
        self.assertIn('https://doris.apache.org/zh-CN/docs/4.x/test/other#test', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn('SELECT 1 &lt; 2;', rendered)

    def test_exact_faq_excludes_neighboring_error_but_preserves_candidates(self):
        from test_query_pipeline import row
        question = 'Routine Load 无法获取 Kafka 最新的 Offset。'
        primary = {**row('**问题描述**：' + question + '\n**常见原因**：网络不通或第三方库超时。', '4.x'),
                   'contexts': [], 'retrieval_trace': [{'subquery_id': 'q1'}]}
        neighbor = {**row('**问题描述**：Offset out of range。', '4.x', 1), 'rank': 2,
                    'contexts': [], 'retrieval_trace': [{'subquery_id': 'q1'}]}
        retriever = Mock(manifest={'build_id': 'fixture'})
        retriever.search_plan.return_value = [primary, neighbor]
        with patch('doris_rag.pipeline.enrich_context', side_effect=lambda rows, query: rows), patch(
                'doris_rag.pipeline.answer', return_value={'status': 'generated', 'citation_numbers': [1]}) as generate:
            result = run_query(question, retriever=retriever)
        self.assertEqual(len(generate.call_args.args[1]), 1)
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(len(result['retrieval_candidates']), 2)
        self.assertFalse(result['retrieval_candidates'][1]['selected_for_generation'])
        # 仅报告网络异常但提出另一问题时，不能套用单条 FAQ 收窄整个任务。
        with patch('doris_rag.pipeline.enrich_context', side_effect=lambda rows, query: rows), patch(
                'doris_rag.pipeline.answer', return_value={'status': 'generated', 'citation_numbers': [1]}) as generate:
            run_query('Routine Load 的 Offset 有哪些常见报错？', retriever=retriever)
        self.assertEqual(len(generate.call_args.args[1]), 2)

    def test_same_document_independent_facts_and_cross_version_sources_survive(self):
        def row(rank, doc, version, text, start):
            return {'rank': rank, 'doc_id': doc, 'source_version': version, 'chunk_text': text,
                    'source_start_line': start, 'source_end_line': start,
                    'source_url': 'https://doris.apache.org/zh-CN/docs/' + version + '/page',
                    'contexts': [{'text': '共享背景\n事实一\n事实二\n', 'source_start_line': 1, 'source_end_line': 3}]}
        rows = [row(1, 'd4', '4.x', '事实一', 2), row(2, 'd4', '4.x', '事实二', 3),
                row(3, 'd3', '3.x', '事实一', 2)]
        groups = evidence_groups(rows)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]['ranks'], [1, 3])
        self.assertEqual([s['source_version'] for s in groups[0]['sources']], ['4.x', '3.x'])
        for group in groups:
            for context in group['contexts']:
                self.assertNotIn('事实一', context['html'])
                if context['doc_id'] == 'd4':
                    self.assertNotIn('事实二', context['html'])


if __name__ == '__main__':
    unittest.main()
