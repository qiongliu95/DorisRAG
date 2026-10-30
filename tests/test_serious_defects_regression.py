"""真实失败的冻结提取/候选回归；模型外发禁止，离线通过不代表真实回答通过。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from doris_rag import generation, telemetry
from doris_rag.context import enrich_context
from doris_rag.pipeline import enrich_selection
from doris_rag.query_understanding import understand_query, _foundation_query, ExtractionValidationError
from doris_rag.retrieval import explicit_named_terms
from test_query_pipeline import fake_retriever, row

ASSET=Path(__file__).parent/'fixtures/serious_defects_cases.json'
CONVERSATIONS=Path(__file__).parent/'fixtures/session_relation_cases.json'


class SeriousDefectRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset=json.loads(ASSET.read_text(encoding='utf-8'))

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch('requests.sessions.Session.request',side_effect=AssertionError('offline_no_network')).start()
        patch('doris_rag.query_understanding.llm_config',return_value={'fixture':True}).start()

    def test_CONV006_original_order_and_business_assertions_registered(self):
        c=next(c for c in json.loads(CONVERSATIONS.read_text(encoding='utf-8')) if c['id']=='CONV006')
        self.assertEqual(c['inputs'],[
            '列转行和聚合多维分析有什么区别','一般在什么场景下使用？',
            'Doris 4.0.2 中 hive.recursive_directories 有什么作用和使用条件？','所有版本做个比较。',
            'Doris 的 BloomFilter 索引有什么限制？','其他版本呢？'])
        self.assertEqual(len(c['expected']),6)
        self.assertEqual([e['subjects'] for e in c['expected']],
            [['列转行','聚合多维分析']]*2+[['hive.recursive_directories']]*2+[['BloomFilter']]*2)
        self.assertTrue(all(e['business'] for e in c['expected']))
        self.assertIn('unittest discover',c['regression_entrypoint'])

    def test_D02_saved_real_extractions_enter_valid_scoped_queries(self):
        for sample in self.asset['D02']['historical_samples']:
            with self.subTest(case=sample['id']):
                data=sample['input'];untouched=copy.deepcopy(data)
                with patch('doris_rag.query_understanding.chat_completion',side_effect=sample['responses']):
                    p=understand_query(data['query'],data['selected_version'],data.get('analysis_seed'),
                        version_scope=data.get('version_scope'),resolved_scope=data.get('resolved_scope'),task=data.get('task'))
                self.assertEqual(p['status'],'ready',p)
                self.assertEqual(p['versions'],data['resolved_scope']['versions'])
                self.assertTrue(any('enable_insert_strict' in q['text'] for q in p['subqueries']))
                if sample['id']=='A18_6':
                    # 当前环境是合法原始事实；隔离约束检查实际检索来源，而不是删除该事实。
                    self.assertIn('2.1.4',data['query'])
                    self.assertNotIn('2.1.4','\n'.join(q['text'].partition(' 用户条件：')[0] for q in p['subqueries']))
                    engine=fake_retriever([row('enable_insert_strict 的定义。',v,n)
                                           for n,v in enumerate(('2.1','3.x','4.x'))])
                    with telemetry.request_trace(True,persist=False) as trace:
                        result=engine.search_plan(p,5,mode='dense')
                    self.assertEqual({e['version'] for e in trace.data['events'] if e['name']=='retrieval.scope_filter'}, {'3.x','4.x'})
                    self.assertTrue(all(r['source_version'] in ('3.x','4.x') for r in result))
                self.assertEqual(data,untouched)

    def test_D02_parameter_reference_is_not_an_enabled_mechanism(self):
        basis={'base_object':'导入操作','scenario_object':'option_alpha 配置','type_quote':'',
               'rule_goal':'核对 option_alpha 配置的语义和影响'}
        query='Doris 3.x，我的 option_alpha=false，为什么导入失败？'
        self.assertIn('option_alpha',_foundation_query(basis,query))
        for rule in ('在 option_alpha 下核对导入规则','启用 option_alpha 后核对导入规则'):
            with self.subTest(rule=rule),self.assertRaises(ExtractionValidationError):
                _foundation_query({**basis,'rule_goal':rule},query)
        with self.assertRaises(ExtractionValidationError):
            _foundation_query({**basis,'base_object':'option_alpha 配置导入'},query)

    def test_D05_only_explicit_multiword_names_are_coverage_targets(self):
        self.assertEqual(explicit_named_terms({'kind':'knowledge','raw_query':'Alpha Tracker Limiter 和 Alpha Tracker 有何区别？'}),['Alpha Tracker Limiter'])
        self.assertEqual(explicit_named_terms({'kind':'scenario','raw_query':'Alpha Tracker Limiter 出错了'}),[])
        self.assertEqual(explicit_named_terms({'kind':'knowledge','raw_query':'这个功能是什么？'}),[])

    def test_D05_existing_low_rank_name_gets_slot_without_changing_capacity_or_scope(self):
        rows=[row('普通资料 '+str(i),'3.x',i) for i in range(8)]
        rows[-1]['chunk_text']='Alpha Beta Limiter 的独立定义与用途。'
        engine=fake_retriever(rows)
        p=understand_query('Doris 3.x 中 Alpha Beta Limiter 的用途是什么？')
        selected=engine.search_plan(p,5,mode='dense')
        self.assertLessEqual(len([r for r in selected if not r.get('reference_root')]),5)
        self.assertIn(rows[-1]['chunk_id'],[r['chunk_id'] for r in selected])
        self.assertTrue(all(r['source_version']=='3.x' for r in selected))
        p=understand_query('Doris 3.x 中 Missing Beta Limiter 的用途是什么？')
        selected=engine.search_plan(p,5,mode='dense')
        self.assertFalse(any('Missing Beta Limiter' in r['chunk_text'] for r in selected))

    def test_D05_frozen_candidate_pools_preserve_all_required_facts_in_messages(self):
        for case in self.asset['D05']:
            with self.subTest(case=case['id']),telemetry.request_trace(True,persist=False) as trace:
                candidates=copy.deepcopy(case['candidate_rows'])
                engine=fake_retriever(candidates)
                index={r['chunk_id']:i for i,r in enumerate(candidates)}
                groups={g['version']:g['rows'] for g in case['historical_groups']}
                def rank(query,version,*args):
                    return [{'index':index[r['chunk_id']],'score':r['score'],'rank':n,'channel_hits':[]}
                            for n,r in enumerate(groups[version],1)]
                engine._rank=Mock(side_effect=rank)
                plan=copy.deepcopy(case['historical_plan']);pool=[]
                selected=engine.search_plan(plan,5,candidate_rows=pool)
                final=enrich_selection(selected,pool,case['question'],plan)
                built=generation.messages(case['question'],final,','.join(plan['versions']),plan)
                for ref in case['references']:
                    self.assertIn(ref['quote'],built[-1]['content'],ref['chunk_id'])
                self.assertLessEqual(sum(len(c['text']) for r in final for c in r.get('contexts',[])),14000)
                self.assertTrue(all(len(c['text'])<=4000 for r in final for c in r.get('contexts',[])))
                self.assertTrue(all(r['source_version'] in plan['versions'] for r in final))
                primary=next(e for e in trace.data['events'] if e['name']=='retrieval.primary_selection')
                self.assertLessEqual(primary['count'],5)

    def test_D05_long_table_row_is_fetched_before_unrelated_table_prefix(self):
        case=next(c for c in self.asset['D05'] if c['id']=='S068')
        enriched=enrich_context(copy.deepcopy(case['historical_selected']),case['question'])
        first=enriched[0]
        target=next(c for c in first['contexts'] if c['kind']=='selected_table_row')
        self.assertTrue(target['complete'])
        self.assertEqual(target['source_start_line'],first['source_start_line'])
        self.assertIn(case['references'][0]['quote'],target['text'])
        self.assertEqual(target['source_sha256'],first['source_sha256'])

    def test_D03_stopped_failure_and_both_scopes_remain_reviewable(self):
        case=self.asset['D03'];text='\n'.join(r['chunk_text']+'\n'+'\n'.join(c['text'] for c in r.get('contexts',[])) for r in case['evidence_rows'])
        self.assertTrue(case['repair_blocked_at_historical_limit'])
        self.assertEqual(case['historical_verdict'],'FAIL')
        self.assertIn('UPDATE_FIXED_COLUMNS',text)
        self.assertIn('UPDATE_FLEXIBLE_COLUMNS',text)
        self.assertIn('目前只有 stream load',text)
        self.assertIn('partial_columns=true',case['input'])
