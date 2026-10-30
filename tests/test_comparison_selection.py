"""比较对象覆盖与补取后完整包含去重：检查最终证据，而非仅候选命中。"""
import unittest
from unittest.mock import Mock, patch

from doris_rag.context import contained_evidence
from doris_rag.pipeline import run_query
from doris_rag.query_understanding import understand_query
from doris_rag.retrieval import comparison_objects
from doris_rag.telemetry import request_trace
from test_query_pipeline import fake_retriever, row
from test_ra_mechanisms import ranking


def evidence(text, n, doc='same', **extra):
    return {**row(text, number=n), 'doc_id':doc, 'contexts':[], 'context_status':'complete', **extra}


class ComparisonSelectionTests(unittest.TestCase):
    def test_single_query_reserves_real_evidence_for_both_objects(self):
        rows = [evidence('Alpha 的独立事实'+str(n), n) for n in range(5)] + [evidence('Beta 的定义和场景。', 5, 'other')]
        engine = fake_retriever(rows)
        engine._rank = Mock(return_value=ranking(list(range(6))))
        plan = understand_query('Alpha和Beta有什么区别，在什么场景使用？','3.x')
        self.assertEqual(len(plan['subqueries']),1)
        found = engine.search_plan(plan,5)
        self.assertEqual(len(found),5)
        self.assertIn(rows[5]['chunk_id'], {r['chunk_id'] for r in found})
        self.assertGreater(sum(r['doc_id']=='same' for r in found),1)
        self.assertEqual(engine._rank.call_count,1)

    def test_same_document_can_cover_four_independent_objects(self):
        rows = [evidence('ROLLUP 层次汇总。',0), evidence('ROLLUP 的另一个事实。',1),
                evidence('CUBE 独立维度组合。',2), evidence('GROUPING SETS 显式集合。',3),
                evidence('GROUPING_ID 位图标识。',4), evidence('其他事实。',5)]
        engine = fake_retriever(rows)
        engine._rank = Mock(return_value=ranking(list(range(6))))
        found = engine.search('ROLLUP、CUBE、GROUPING SETS和GROUPING_ID有什么区别？','3.x',5)
        self.assertEqual(len(found),5)
        self.assertEqual({r['doc_id'] for r in found},{'same'})
        self.assertTrue(all(any(obj in r['chunk_text'] for r in found) for obj in ('ROLLUP','CUBE','GROUPING SETS','GROUPING_ID')))

    def test_missing_object_and_title_only_do_not_manufacture_coverage(self):
        rows = [evidence('Alpha 的有效事实。',0), evidence('## Beta',1,'other',title='Beta')]
        engine = fake_retriever(rows)
        engine._rank = Mock(return_value=ranking([0,1]))
        with request_trace(True,persist=False) as trace:
            found = engine.search('Alpha和Beta有什么区别？','3.x',5)
        beta = next(e for e in trace.data['events'] if e['name']=='retrieval.comparison_coverage' and e['subject']=='Beta')
        self.assertFalse(beta['covered'])
        self.assertEqual(beta['reason'],'no_matching_candidate')
        self.assertEqual(len(found),1)

    def test_scope_comparison_and_ordinary_questions_are_not_object_tasks(self):
        for text in ('Doris 2.1和3.x有什么区别？','Alpha如何使用？','Alpha和Beta的配置说明'):
            self.assertEqual(comparison_objects(understand_query(text,'all')),[])

    def test_containment_requires_all_content_and_same_verified_scope(self):
        base = evidence('A定义。',0,contexts=[{'text':'A定义。B事实。','source_sha256':'test'}])
        duplicate = evidence('B事实。',1)
        self.assertIn(duplicate['chunk_id'],contained_evidence([base,duplicate]))
        for changed in ({'contexts':[{'text':'独立的新条件。','source_sha256':'test'}]},
                        {'source_version':'2.1'}, {'source_sha256':'different'}, {'source_sha256':None},
                        {'context_status':'partial'}, {'scope_ids':['another']}, {'tab_labels':['Another']},
                        {'version_note':['2.5起支持。']}, {'reference_root':True}):
            with self.subTest(changed=changed):
                self.assertFalse(contained_evidence([base,{**duplicate,**changed}]))

    def test_refill_from_existing_pool_reaches_final_model_evidence(self):
        rows = [evidence('Alpha定义。',0),evidence('Beta事实。',1),evidence('Gamma规则。',2,'c'),
                evidence('Delta方法。',3,'d'),evidence('Epsilon场景。',4,'e'),evidence('Zeta限制。',5)]
        rows[0]['contexts']=[{'text':'Alpha定义。Beta事实。','source_sha256':'test'}]
        engine = fake_retriever(rows)
        engine._rank = Mock(return_value=ranking(list(range(6))))
        def enrich(items, query):
            return [{**r,'context_status':'complete','contexts':rows[int(r['chunk_id'].rsplit(':',1)[1])]['contexts']} for r in items]
        with request_trace(True,persist=False) as trace, patch('doris_rag.pipeline.enrich_context',side_effect=enrich), patch(
                'doris_rag.pipeline.answer',side_effect=AssertionError('no model')):
            result = run_query('知识规则说明', '3.x', retriever=engine, evidence_only=True)
        sent = next(e['data'] for e in trace.data['events'] if e['name']=='generation.input_rows')
        self.assertEqual(len(sent),5)
        self.assertNotIn(rows[1]['chunk_id'],{r['chunk_id'] for r in sent})
        self.assertIn(rows[5]['chunk_id'],{r['chunk_id'] for r in sent})
        self.assertEqual(engine._rank.call_count,1)
        discarded = next(r for r in result['retrieval_candidates'] if r['chunk_id']==rows[1]['chunk_id'])
        self.assertFalse(discarded['selected_for_generation'])
        self.assertTrue(any(e['name']=='augmentation.refill' for e in trace.data['events']))

    def test_pool_exhaustion_leaves_fewer_evidence_units(self):
        from doris_rag.pipeline import enrich_selection
        rows = [evidence('定义。事实。',0),evidence('事实。',1)]
        with patch('doris_rag.pipeline.enrich_context',side_effect=lambda rs,q:rs):
            found = enrich_selection(rows,[], '规则说明')
        self.assertEqual(len(found),1)

    def test_duplicate_slots_do_not_introduce_new_unrequested_documents(self):
        from doris_rag.pipeline import enrich_selection
        rows = [evidence('Alpha定义。补充事实。',0), evidence('补充事实。',1)]
        reserve = [evidence('另一函数的示例引用Alpha，但不提供Alpha的独立定义。',2,'other')]
        with patch('doris_rag.pipeline.enrich_context',side_effect=lambda rs,q:rs):
            found = enrich_selection(rows,reserve,'Alpha是什么？',understand_query('Alpha是什么？','3.x'))
        self.assertEqual([r['doc_id'] for r in found],['same'])
