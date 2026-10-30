"""提取结构修复次数有界，事实与版本校验不因重试而放宽。"""
import json
import unittest
from unittest.mock import patch
from doris_rag.query_understanding import understand_query

class ExtractionRepairTests(unittest.TestCase):
    query='Doris 2.1，Hive目录读取失败，hive.recursive_directories=true，为何未生效？'
    def value(self, valid=False):
        return json.dumps({'goal':'核对目录读取规则和参数条件','facts':[],'conditions':[],
            'inferences':[],'missing_conditions':[],
            'foundation':{'base_object':'目录' if valid else 'Hive目录','scenario_object':'Hive目录',
                'type_quote':'','rule_goal':'递归遍历目录的基本规则'},
            'subquestions':['hive.recursive_directories参数在指定版本的适用条件是什么？']},ensure_ascii=False)
    def test_repair_once_preserves_scope(self):
        with patch('doris_rag.query_understanding.chat_completion',side_effect=[self.value(),self.value(True)]) as call:
            result=understand_query(self.query)
        self.assertEqual(call.call_count,2)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['versions'],['2.1'])
    def test_second_invalid_result_still_fails_closed(self):
        with patch('doris_rag.query_understanding.chat_completion',return_value=self.value()) as call:
            result=understand_query(self.query)
        self.assertEqual(call.call_count,2)
        self.assertEqual(result['status'],'understanding_failed')
    def test_transport_failure_does_not_trigger_contract_retry(self):
        with patch('doris_rag.query_understanding.chat_completion',side_effect=ConnectionError) as call:
            result=understand_query(self.query)
        self.assertEqual(call.call_count,1)
        self.assertEqual(result['status'],'understanding_failed')
    def test_rejected_mechanism_does_not_drop_literal_parameters(self):
        query="我的 Doris 4.x 建表失败，PROPERTIES ('variant_enable_doc_mode'='true', 'variant_max_subcolumns_count'='2048')，如何核对限制？"
        value=json.loads(self.value(True))
        value['foundation']={'base_object':'建表','scenario_object':'模式配置','type_quote':'','rule_goal':'建表语法'}
        value['subquestions']=['Doris 3.x 的该模式有什么限制？']
        with patch('doris_rag.query_understanding.chat_completion',return_value=json.dumps(value,ensure_ascii=False)):
            result=understand_query(query)
        self.assertEqual(result['status'],'ready')
        queries=' '.join(x['text'] for x in result['subqueries'])
        self.assertIn('variant_enable_doc_mode',queries)
        self.assertIn('variant_max_subcolumns_count',queries)
        self.assertNotIn('3.x',queries)
        # 正式链路允许最多三个知识子问，另保留一个完整用户任务查询。
        self.assertLessEqual(len([q for q in result['subqueries'] if q['purpose']!='task']),3)
        self.assertLessEqual(len(result['subqueries']),4)
