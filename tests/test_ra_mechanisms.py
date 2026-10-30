"""基础对象作用域与限制补取的通用机制反例；不发送模型请求。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from doris_rag.common import sha256_file
from doris_rag.context import enrich_context
from doris_rag.documents import DOCS_PATH, load_document
from doris_rag.query_understanding import understand_query
from test_query_pipeline import fake_retriever, row


def ranking(ids):
    return [{'index': i, 'rank': n, 'score': 1/(60+n), 'channel_hits': []} for n, i in enumerate(ids, 1)]


class MechanismTests(unittest.TestCase):
    def plan(self):
        plan = understand_query('两个目标', '3.x')
        plan['subqueries'] = [
            {'id': 'q1', 'text': 'Doris Acme Driver连接 的基础使用方法是什么？', 'purpose': 'foundation', 'technical_literals': []},
            {'id': 'q2', 'text': '操作如何加速？', 'purpose': 'scenario', 'technical_literals': []}]
        return plan

    def rows(self):
        rows = [row('高排名的场景操作', number=0), row('Acme Driver 推荐连接方式。', number=1), row('别的操作', number=2)]
        for n,r in enumerate(rows):
            r['doc_id'] = str(n)
        rows[1].update(tab_labels=['Client'], scope_ids=['client-scope'])
        return rows

    def test_foundation_scope_is_preserved_within_original_k(self):
        rows = self.rows()
        engine = fake_retriever(rows)
        engine._rank = Mock(side_effect=[ranking([0, 2, 1]), ranking([0, 2])])
        found = engine.search_plan(self.plan(), 2)
        self.assertEqual({r['doc_id'] for r in found}, {'0', '1'})
        self.assertEqual(len(found), 2)
        self.assertTrue(any(a.get('role') == 'foundation_scope' for r in found for a in r['context_anchors']))

    def test_unrelated_tab_is_not_a_coverage_requirement(self):
        rows = self.rows()
        rows[1]['chunk_text'] = 'Acme 数据导入，只有对象名相同。'
        engine = fake_retriever(rows)
        engine._rank = Mock(side_effect=[ranking([0, 2, 1]), ranking([0, 2])])
        found = engine.search_plan(self.plan(), 2)
        self.assertNotIn('1', {r['doc_id'] for r in found})

    def test_existing_document_uses_scope_anchor_not_an_extra_slot(self):
        rows = self.rows()
        rows[1]['doc_id'] = rows[0]['doc_id']
        engine = fake_retriever(rows)
        engine._rank = Mock(side_effect=[ranking([0, 1]), ranking([0, 2])])
        found = engine.search_plan(self.plan(), 1)
        self.assertEqual(len(found), 1)
        self.assertTrue(any(a.get('role') == 'foundation_scope' for a in found[0]['context_anchors']))

    def test_recalled_conditions_keep_own_feature_and_implementation_scope(self):
        rows = [row('操作方法', number=0)]
        rows[0]['section_paths'] = ['对象 > 特性甲 > 操作']
        for number, (family, label, closed) in enumerate([('特性甲', [], True), ('特性乙', [], True),
                                                         ('特性甲', ['Other'], True), ('特性甲', [], False)], 1):
            candidate = row('有事实的条件说明', number=number)
            candidate.update(doc_id=rows[0]['doc_id'], section_paths=[f'对象 > {family} > 条件'],
                tab_labels=label, scope_ids=[str(number)], applicability_conditions=[
                    {'scope_id': str(number), 'kind': 'admonition', 'closed': closed}])
            rows.append(candidate)
        engine = fake_retriever(rows)
        engine._rank = Mock(return_value=ranking(list(range(len(rows)))))
        found = engine.search('操作方法', '3.x', 1)
        anchors = [a for a in found[0]['context_anchors'] if a.get('role') == 'recalled_constraint']
        self.assertEqual([a['scope_ids'] for a in anchors], [['1']])
        self.assertEqual(len(found), 1)

    def context_fixture(self, directory, long_intro=False):
        path = directory / DOCS_PATH / 'version-3.x/source.md'
        path.parent.mkdir(parents=True)
        path.write_text('定义\n## 功能\n' + ('普通前言。\n'*1000 if long_intro else '2.5 起的功能。\n') +
                        ':::caution\n仅在条件成立时使用。\n:::\n### 操作\n操作正文。\n', encoding='utf-8')
        parsed = load_document(path, '3.x', directory)
        scope = next(s for s in parsed['scopes'] if s['kind'] == 'admonition')
        operation = next(s for s in parsed['sections'] if s['label'] == '操作')
        item = {**row('操作正文。'), 'original_path': str(path), 'source_sha256': sha256_file(path),
            'parent_section_id': operation['section_id'], 'related_context': operation,
            'context_anchors': [{'scope_ids': [scope['scope_id']], 'subquery_id': 'q2', 'role': 'recalled_constraint'}]}
        return item

    def test_constraint_keeps_feature_heading_and_version_under_same_budget(self):
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root': temp}):
            item = self.context_fixture(Path(temp))
            result = enrich_context([item], '有什么条件？', max_chars=120)[0]
            first = result['contexts'][0]
            self.assertTrue(first['kind'].startswith('recalled_constraint:功能'))
            self.assertIn('## 功能', first['text'])
            self.assertIn('2.5 起', first['text'])
            self.assertIn('仅在条件成立时使用。', first['text'])
            self.assertLessEqual(sum(len(c['text']) for c in result['contexts']), 120)

    def test_long_preface_cannot_push_actual_constraint_out_of_scope_budget(self):
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root': temp}):
            item = self.context_fixture(Path(temp), long_intro=True)
            result = enrich_context([item], '有什么条件？', max_chars=100)[0]
            self.assertIn('仅在条件成立时使用。', result['contexts'][0]['text'])
            self.assertLessEqual(sum(len(c['text']) for c in result['contexts']), 100)

    def test_insufficient_budget_remains_explicitly_partial(self):
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root': temp}):
            item = self.context_fixture(Path(temp))
            result = enrich_context([item], '有什么条件？', max_chars=4)[0]
            self.assertEqual(result['context_status'], 'partial')
            self.assertTrue(result['context_issues'])
            self.assertLessEqual(sum(len(c['text']) for c in result['contexts']), 4)


if __name__ == '__main__':
    unittest.main()
