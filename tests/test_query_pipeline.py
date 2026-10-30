import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import faiss
import numpy as np

from doris_rag.common import sha256_file
from doris_rag.context import enrich_context
from doris_rag.generation import answer, llm_config, messages
from doris_rag.lexical import exact_literals
from doris_rag.pipeline import run_query
from doris_rag.query_understanding import is_scenario, technical_literals, understand_query, version_mentions
from doris_rag.retrieval import Retriever, rrf
from test_pipeline import CharacterTokenizer, document


def row(text, version="3.x", number=0):
    return {**document(text), "chunk_id": f"fixture:{version}:{number}", "source_version": version,
            "section_path": "测试 > 参数", "section_paths": ["测试 > 参数"], "chunk_text": text,
            "rank": 1, "split_kind": "structure", "context_status": "complete"}


def fake_retriever(rows):
    retrieval = Retriever.__new__(Retriever)
    retrieval.rows = rows
    retrieval.index = faiss.IndexFlatIP(2)
    retrieval.index.add(np.tile(np.array([[1.0, 0.0]], dtype=np.float32), (len(rows), 1)))
    retrieval.encoder = Mock(tokenizer=CharacterTokenizer(), max_seq_length=512)
    retrieval.encoder.encode.side_effect = lambda texts, **kw: np.tile(np.array([[1.0,0.0]],dtype=np.float32),(len(texts),1))
    retrieval.manifest = {"build_id": "synthetic-unit-fixture", "query_prefix": "检索："}
    retrieval.version_indexes, retrieval.lexical = {}, None
    return retrieval


class UnderstandingTests(unittest.TestCase):
    def test_short_followup_keeps_foundation_target(self):
        seed={'goal':'核对规则','facts':[],'conditions':[],'follow_up':'那边界呢？',
              'subqueries':[{'id':'q1','text':'区间定义','purpose':'foundation','technical_literals':[]}]}
        plan=understand_query('Doris 3.x，继续核对规则',analysis_seed=seed)
        self.assertEqual(plan['subqueries'][0]['purpose'],'foundation')
        self.assertFalse(plan['llm_used'])

    def test_auto_version_and_patch_literal(self):
        plan = understand_query("Doris3.0.2 的 enable_insert_strict 是什么？")
        self.assertEqual(plan["versions"], ["3.x"])
        self.assertEqual(plan["version_mentions"][0]["literal"], "3.0.2")
        self.assertIn("enable_insert_strict", plan["technical_literals"])

    def test_version_selection_conflict_needs_clarification(self):
        self.assertEqual(understand_query("Doris 3.x 参数说明", "4.x")["status"], "needs_clarification")
        self.assertEqual(understand_query("2.1和3.x的区别", "2.1")["status"], "needs_clarification")

    def test_comparison_independent_of_mode(self):
        self.assertEqual(understand_query("Doris2.1与3.x参数有什么变化")["versions"], ["2.1", "3.x"])

    def test_comparison_synonyms_keep_explicit_version_scope_without_model(self):
        for query in ("Doris 2.1.4与3.x的默认值有何不同？", "2.1与3.x行为有什么不同？",
                      "Doris 2.1和4.x的限制是否一致？", "Compare Doris 2.1 and 3.x behavior",
                      "How do Doris 2.1 and 3.x differ?"):
            with self.subTest(query=query), patch('doris_rag.query_understanding.chat_completion', side_effect=AssertionError('no model')):
                plan = understand_query(query, 'all')
                self.assertEqual(plan['status'], 'ready')
                self.assertEqual(plan['versions'], ['2.1', '4.x'] if '4.x' in query else ['2.1', '3.x'])
                self.assertFalse(plan['llm_used'])
        # 只有多版本并列而无比较目标仍需澄清，驱动版本也不能扩成Doris来源。
        self.assertEqual(understand_query('Doris 2.1和3.x的配置', 'all')['status'], 'needs_clarification')
        self.assertEqual(version_mentions('Different MySQL 5.1 drivers for Doris 3.x'),
                         [{'literal': '3.x', 'family': '3.x', 'supported_source': True}])

    def test_unspecified_searches_latest_only_and_flags_unknown(self):
        plan = understand_query("PI() 返回什么？")
        self.assertEqual(plan["versions"], ["4.x"])
        self.assertTrue(plan["version_unspecified"])

    def test_mysql_ip_and_pi_do_not_become_doris_versions(self):
        self.assertEqual(version_mentions("MySQL 3.1 驱动连接 10.3.1.2，PI=3.14159265"), [])

    def test_upgrade_log_http_version_does_not_become_doris_version(self):
        self.assertEqual([v['literal'] for v in version_mentions('Doris 4.1.4 升级后 POST /api/upload HTTP/1.1\nHTTP/1.1 404 Not Found')], ['4.1.4'])

    def test_generic_uppercase_words_are_not_exact_identifiers(self):
        self.assertEqual(technical_literals('JDBC INT HTTP SQL'), [])
        self.assertIn('enable_insert_strict', technical_literals('JDBC enable_insert_strict=false'))
        self.assertIn('PI', technical_literals('SQL SELECT PI()'))

    def test_parameter_touching_chinese_keeps_ascii_identifier_boundaries(self):
        self.assertEqual(technical_literals('参数option_alpha的含义'), ['option_alpha'])
        self.assertEqual(technical_literals('Doris 3.x中option_alpha参数的规则'), ['option_alpha'])
        self.assertEqual(technical_literals('前缀my_option_alpha后缀'), ['my_option_alpha'])

    def test_out_of_scope_version_is_not_silently_defaulted(self):
        self.assertEqual(understand_query("Doris 2.0 的参数")["status"], "needs_clarification")

    def test_function_and_error_literals(self):
        literals = technical_literals('PI() 报错 "Access denied for user test" [E-1001]')
        self.assertIn("PI", literals)
        self.assertIn("[E-1001]", literals)
        self.assertIn("Access denied for user test", literals)

    def analyze(self, payload):
        query = "Doris 3.0.2 集群执行失败，日志如下：\nERROR: [E-1001]\nenable_insert_strict=false\n如何排查？"
        with patch("doris_rag.query_understanding.llm_config", return_value={"configured": True}), patch(
                "doris_rag.query_understanding.chat_completion", return_value=json.dumps(payload, ensure_ascii=False)):
            return query, understand_query(query)

    def payload(self):
        return {"goal": "查文档排查导入失败", "facts": [{"quote": "ERROR: [E-1001]"}],
                "conditions": [{"quote": "enable_insert_strict=false"}], "inferences": ["原因尚未证实"],
                "foundation":{"base_object":"enable_insert_strict","scenario_object":"","type_quote":"","rule_goal":"作用"},
                "missing_conditions": ["缺少完整错误上下文"], "subquestions": ["[E-1001] 错误如何解释？", "enable_insert_strict 有什么限制？"]}

    def test_llm_quotes_are_located_and_conditions_participate_in_queries(self):
        query, plan = self.analyze(self.payload())
        self.assertEqual(plan["status"], "ready")
        fact = plan["facts"][0]
        self.assertEqual(query[fact["start"]:fact["end"]], fact["quote"])
        self.assertNotIn("enable_insert_strict=false", plan["subqueries"][0]["text"])
        self.assertIn("enable_insert_strict=false", plan["subqueries"][1]["text"])
        self.assertEqual(plan['subqueries'][0]['purpose'],'foundation')
        self.assertEqual(plan['subqueries'][1]['technical_literals'],['[E-1001]'])
        self.assertTrue(plan["inferences"])

    def test_fabricated_user_fact_rejected(self):
        value = self.payload()
        value["facts"] = [{"quote": "BE 内存不足已确认"}]
        self.assertEqual(self.analyze(value)[1]["status"], "understanding_failed")

    def test_foundation_rejects_fabricated_type_and_scenario_qualification(self):
        value=self.payload();value['foundation']['type_quote']='BIGINT'
        self.assertEqual(self.analyze(value)[1]['status'],'understanding_failed')
        value=self.payload();value['foundation']['scenario_object']='enable_insert_strict'
        self.assertEqual(self.analyze(value)[1]['status'],'understanding_failed')

    def test_fabricated_parameter_and_version_rejected(self):
        for question in ("new_fake_parameter 为什么失败？", "Doris 4.1.4 有什么限制？"):
            value = self.payload()
            value["foundation"]["base_object"] = question
            value["subquestions"] = []
            self.assertEqual(self.analyze(value)[1]["status"], "understanding_failed")

    def test_invalid_subquestion_does_not_discard_independent_valid_queries(self):
        value = self.payload()
        value['subquestions'] = ['new_fake_parameter 为什么失败？', 'enable_insert_strict 有什么限制？']
        plan = self.analyze(value)[1]
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(len(plan['subqueries']), 2)
        self.assertNotIn('new_fake_parameter', plan['subqueries'][0]['text'])
        self.assertTrue(plan['extraction_warnings'])

    def test_observations_without_first_person_still_need_analysis(self):
        for query in ('Doris 3.1 配置 partial_columns=true，其他列变成NULL，如何解释？',
                      'Doris 3.1，Routine Load 配了 partial_columns=true，消息只有 id 和 age，其他列变成NULL。',
                      'Doris 2.1.1 查询结束后BE内存没降，RSS与Tracker不一致并出现OOM。',
                      'Doris 2.1.6 分区写入失败，如何核对边界？'):
            self.assertTrue(is_scenario(query))
        self.assertFalse(is_scenario('Doris 4.1.4 参数设0或负数分别怎样？'))

    def test_long_independent_multiple_targets_need_analysis_but_not_repeated_goal(self):
        query = 'Doris 4.x 通过 MySQL JDBC 连接时，向量查询如何使用 Prepared Statement？哪些相关命令不能直接用 mysql 客户端执行，连接参数怎么写？'
        self.assertTrue(is_scenario(query))
        self.assertFalse(is_scenario('当前目标：' + 'PI() 返回什么？' * 10))

    def test_questions_cannot_be_saved_as_reported_facts(self):
        value = self.payload()
        value['facts'] = [{'quote': '如何排查？'}, {'quote': 'ERROR: [E-1001]'}]
        plan = self.analyze(value)[1]
        self.assertEqual([f['quote'] for f in plan['facts']], ['ERROR: [E-1001]'])

    def test_missing_llm_does_not_embed_raw_logs(self):
        retrieval = Mock()
        with patch("doris_rag.query_understanding.llm_config", return_value={"key": ""}):
            result = run_query("日志如下：\n" + "ERROR [E-1001]\n" * 100, retriever=retrieval)
        self.assertEqual(result["status"], "understanding_unavailable")
        retrieval.search_plan.assert_not_called()

    def test_invalid_llm_json_is_explicit(self):
        with patch("doris_rag.query_understanding.llm_config", return_value={"key": True}), patch(
                "doris_rag.query_understanding.chat_completion", return_value="not json"):
            self.assertEqual(understand_query("配置如下：a=1\n执行失败")["status"], "understanding_failed")


class RetrievalTests(unittest.TestCase):
    def test_document_support_split_across_chunks_keeps_source(self):
        rows=[row('noise',number=0),row('first rule',number=1),row('second rule',number=2)]
        rows[0]['doc_id']='noise-doc';rows[1]['doc_id']=rows[2]['doc_id']='rules-doc'
        retrieval=fake_retriever(rows)
        def ranking(ids):return [{'index':i,'rank':n,'score':1/(60+n),'channel_hits':[]} for n,i in enumerate(ids,1)]
        retrieval._rank=Mock(side_effect=[ranking([1,0]),ranking([2,0])])
        plan=understand_query('两个目标','3.x');plan['subqueries'].append({'id':'q2','text':'第二目标','technical_literals':[]})
        found=retrieval.search_plan(plan,2)
        self.assertIn('rules-doc',{r['doc_id'] for r in found})
        chosen=next(r for r in found if r['doc_id']=='rules-doc')
        self.assertTrue(chosen['context_anchors'])

        # 最强文档已覆盖时，应保留下一个跨子问题来源，而不是浪费共识覆盖名额。
        retrieval._rank=Mock(side_effect=[ranking([0,1]),ranking([0,2])])
        found=retrieval.search_plan(plan,2)
        self.assertEqual({r['doc_id'] for r in found},{'noise-doc','rules-doc'})

    def test_explicit_reference_is_bounded_and_version_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            rows=[]
            for n,name in enumerate(('a','b','c')):
                item=row('详情请参阅[规则](b.md)' if name=='a' else '详情请参阅[其他](c.md)',number=n)
                item.update(doc_id=name,original_path=str(Path(temp)/(name+'.md')))
                rows.append(item)
            retrieval=fake_retriever(rows)
            plan=understand_query('规则','3.x');plan['subqueries'][0]['purpose']='foundation'
            found=retrieval.search_plan(plan,1)
            self.assertEqual([r['doc_id'] for r in found],['a','b'])
            self.assertTrue(found[1]['reference_root'])
            self.assertEqual(found[1]['context_reference']['from_chunk_id'],rows[0]['chunk_id'])
            rows[1]['source_version']='2.1';retrieval=fake_retriever(rows)
            self.assertEqual(len(retrieval.search_plan(plan,1)),1)

    def test_selected_reference_does_not_require_top_three_foundation_rank(self):
        with tempfile.TemporaryDirectory() as temp:
            rows=[row('详情请参阅[规则](b.md)',number=0),row('基础规则',number=1)]
            for item,name in zip(rows,('a','b')):item.update(doc_id=name,original_path=str(Path(temp)/(name+'.md')))
            retrieval=fake_retriever(rows)
            retrieval._rank=Mock(return_value=[{'index':i,'rank':n,'score':1/(60+n),'channel_hits':[]}
                                              for i,n in ((0,6),(1,7))])
            plan=understand_query('规则','3.x');plan['subqueries'][0]['purpose']='foundation'
            self.assertEqual([e['doc_id'] for e in retrieval.search_plan(plan,1)],['a','b'])

    def test_comparison_counterpart_is_requested_candidate_and_bounded(self):
        rows=[row('相同主题规则','3.x',0),row('相同主题规则','4.x',1),row('别的主题','4.x',2),
              row('未请求版本','2.1',3)]
        for n,item in enumerate(rows):item.update(doc_id=f'doc-{n}',relative_path='topic.md' if n!=2 else 'other.md')
        retrieval=fake_retriever(rows)
        retrieval._rank=Mock(side_effect=[
            [{'index':0,'rank':1,'score':1/61,'channel_hits':[]}],
            [{'index':2,'rank':1,'score':1/61,'channel_hits':[]},{'index':1,'rank':2,'score':1/62,'channel_hits':[]}]])
        found=retrieval.search('Doris 3.x和4.x规则有什么区别？',None,2,'hybrid_precise')
        self.assertEqual(len(found),3)
        self.assertEqual(found[-1]['selection_reason'],'version_counterpart')
        self.assertEqual(found[-1]['relative_path'],'topic.md')
        self.assertEqual(found[-1]['source_version'],'4.x')
        self.assertEqual({r['source_version'] for r in found},{'3.x','4.x'})
        self.assertTrue(found[-1]['reference_root'])

    def test_complete_string_preserves_punctuation_and_identifier_boundary(self):
        self.assertEqual(exact_literals("[E 1001] enable_insert_strict_mode", ["[E-1001]", "enable_insert_strict"]), [])
        self.assertEqual(exact_literals("[E-1001] enable_insert_strict=false", ["[E-1001]", "enable_insert_strict"]), ["[E-1001]", "enable_insert_strict"])

    def test_rrf_uses_rank_not_raw_score(self):
        fused = rrf([[(0, 999999), (1, 1)], [(1, 0.01)]])
        self.assertEqual(fused[0][0], 1)
        self.assertAlmostEqual(fused[0][1], 1 / 62 + 1 / 61)

    def test_error_exact_channel_distinguishes_same_bm25_tokens(self):
        retrieval = fake_retriever([row("[E 1001]"), row("[E-1001]", number=1)])
        found = retrieval.search("[E-1001] 是什么？", "3.x", 2, "hybrid_precise")
        self.assertEqual(found[0]["chunk_text"], "[E-1001]")
        self.assertIn("exact", [c["channel"] for p in found[0]["retrieval_trace"] for c in p["channels"]])

    def test_comparison_all_modes_keep_both_versions(self):
        retrieval = fake_retriever([row("参数 option_a", "2.1"), row("参数 option_a", "3.x"), row("参数 option_a", "4.x")])
        for mode in ("dense", "lexical", "hybrid", "hybrid_precise"):
            found = retrieval.search("2.1和3.x的option_a有什么区别", None, 3, mode)
            self.assertEqual({r["source_version"] for r in found}, {"2.1", "3.x"})

    def test_comparison_encodes_each_subquery_only_once(self):
        retrieval = fake_retriever([row("PI()", "2.1"), row("PI()", "3.x"), row("PI()", "4.x")])
        retrieval.search("所有版本 PI() 是什么？", None, 3, "hybrid_precise")
        self.assertEqual(retrieval.encoder.encode.call_count, 1)

    def test_subqueries_deduplicate_and_keep_channel_provenance(self):
        retrieval = fake_retriever([row("option_a option_b"), row("option_b", number=1)])
        plan = understand_query("option_a 的说明", "3.x")
        plan["subqueries"].append({"id": "q2", "text": "option_b 的说明", "technical_literals": ["option_b"]})
        found = retrieval.search_plan(plan, 3)
        self.assertEqual(len({r["chunk_id"] for r in found}), len(found))
        self.assertEqual({p["subquery_id"] for r in found for p in r["retrieval_trace"]}, {"q1", "q2"})
        self.assertTrue(all(r["score"] == sum(p["merge_rrf_contribution"] for p in r["retrieval_trace"]) for r in found))

    def test_candidate_overlap_is_not_subquestion_coverage(self):
        retrieval=fake_retriever([row('shared',number=0),row('q1 rule',number=1),row('q2 rule',number=2),row('noise',number=3)])
        def ranking(ids):
            return [{'index':i,'rank':n,'score':1/(60+n),'channel_hits':[]} for n,i in enumerate(ids,1)]
        retrieval._rank=Mock(side_effect=[ranking([1,0,3]),ranking([2,0,3])])
        plan=understand_query('q1规则','3.x')
        plan['subqueries'].append({'id':'q2','text':'q2规则','technical_literals':[]})
        found=retrieval.search_plan(plan,3)
        self.assertTrue({'q1 rule','q2 rule'}.issubset({r['chunk_text'] for r in found}))
        self.assertEqual(retrieval.encoder.encode.call_count,1)

    def test_duplicate_best_hit_does_not_hide_second_query_source(self):
        retrieval=fake_retriever([row('shared',number=0),row('second source',number=1),row('noise',number=2)])
        def ranking(ids):
            return [{'index':i,'rank':n,'score':1/(60+n),'channel_hits':[]} for n,i in enumerate(ids,1)]
        retrieval._rank=Mock(side_effect=[ranking([0,2]),ranking([0,1,2])])
        plan=understand_query('两个目标','3.x');plan['subqueries'].append({'id':'q2','text':'第二目标','technical_literals':[]})
        self.assertEqual({r['chunk_text'] for r in retrieval.search_plan(plan,2)}, {'shared','second source'})

    def test_batch_rejects_an_over_budget_subquery(self):
        retrieval=fake_retriever([row('rule')])
        with self.assertRaises(ValueError):retrieval.query_vectors(['短问题','x'*513])
        retrieval.encoder.encode.assert_not_called()

    def test_single_version_never_returns_other_directory(self):
        retrieval = fake_retriever([row("PI()", "2.1"), row("PI()", "3.x")])
        found = retrieval.search("Doris3.x PI() 返回什么？", None, 3)
        self.assertEqual({r["source_version"] for r in found}, {"3.x"})


class ContextAndAnswerTests(unittest.TestCase):
    def test_version_branch_keeps_foundation_and_sibling_conditions_under_budget(self):
        from doris_rag.documents import DOCS_PATH, load_document
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root': temp}):
            path = Path(temp) / DOCS_PATH / 'version-3.x/source.md'
            path.parent.mkdir(parents=True)
            path.write_text('定义。\n## 统计原理\nDEFINITION_NOT_RSS\n## 统计分析\n'
                            '### 条件\nCHECK_LIMITS\n### 原因\n#### Doris 3.2 之后\nNEW_RULE\n'
                            '#### Doris 3.2 之前\nOLD_RULE\n', encoding='utf-8')
            parsed = load_document(path, '3.x', Path(temp))
            node = next(s for s in parsed['sections'] if s['label'] == 'Doris 3.2 之前')
            evidence = {**row('OLD_RULE'), 'original_path': str(path), 'source_sha256': sha256_file(path),
                        'parent_section_id': node['section_id'], 'related_context': node,
                        'context_anchors': [{'scope_ids': []}]}
            actual = enrich_context([evidence], '两个统计值不一致，能判断原因吗？')[0]
            text = ''.join(c['text'] for c in actual['contexts'])
            for fact in ('DEFINITION_NOT_RSS', 'CHECK_LIMITS', 'NEW_RULE', 'OLD_RULE'):
                self.assertIn(fact, text)
            self.assertLessEqual(sum(len(c['text']) for c in actual['contexts']), 14000)
            limited = enrich_context([evidence], '两个统计值不一致，能判断原因吗？', max_chars=20)[0]
            self.assertLessEqual(sum(len(c['text']) for c in limited['contexts']), 20)
            self.assertEqual(limited['context_status'], 'partial')

    def test_deepseek_extraction_and_answer_have_explicit_thinking_budgets(self):
        from doris_rag.generation import chat_completion
        response=Mock();response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        with patch('doris_rag.generation.llm_config',return_value={'api_key':'fixture','base_url':'https://api.deepseek.com','model':'deepseek-flash'}), patch('doris_rag.generation.requests.post',return_value=response) as post:
            chat_completion([],json_output=True)
            self.assertEqual(post.call_args.kwargs['json']['thinking'],{'type':'disabled'})
            chat_completion([])
            self.assertEqual(post.call_args.kwargs['json']['reasoning_effort'],'low')
            self.assertEqual(post.call_args.kwargs['json']['max_tokens'],6000)

    def test_document_rules_are_fetched_outside_hit_section(self):
        from doris_rag.documents import DOCS_PATH
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root':temp}):
            path=Path(temp)/DOCS_PATH/'version-3.x/source.md';path.parent.mkdir(parents=True)
            path.write_text('## 使用限制\n只支持DATE。\n## 其他\n'+'无关说明'*1500+'\n## 示例\nSELECT 1;\n',encoding='utf-8')
            e={**row('SELECT 1;'),'original_path':str(path),'source_sha256':sha256_file(path),
               'related_context':{'source_start_line':5,'source_end_line':6}}
            text=''.join(c['text'] for c in enrich_context([e],'使用条件是什么？')[0]['contexts'])
            self.assertIn('只支持DATE',text)
            self.assertNotIn('无关说明',text)

    def test_tab_anchor_does_not_fetch_other_client_scope(self):
        from doris_rag.documents import DOCS_PATH,load_document
        with tempfile.TemporaryDirectory() as temp, patch('doris_rag.context.config', return_value={'source_root':temp}):
            path=Path(temp)/DOCS_PATH/'version-3.x/source.md';path.parent.mkdir(parents=True)
            path.write_text('连接说明\n<Tabs>\n<TabItem value="java" label="Java">\nJAVA_ONLY\n</TabItem>\n<TabItem value="go" label="Go">\nGO_ONLY\n</TabItem>\n</Tabs>',encoding='utf-8')
            parsed=load_document(path,'3.x',Path(temp));scope=next(s for s in parsed['scopes'] if s['label']=='Java')
            e={**row('连接说明'),'original_path':str(path),'source_sha256':sha256_file(path),
               'related_context':{'source_start_line':1,'source_end_line':1},
               'context_anchors':[{'scope_ids':[scope['scope_id']]}]}
            text=''.join(c['text'] for c in enrich_context([e],'JDBC连接方法')[0]['contexts'])
            self.assertIn('JAVA_ONLY',text);self.assertNotIn('GO_ONLY',text)

            # 命中Tab入口本身时，无须另一个候选再次指向该Tab。
            e.pop('context_anchors');e.update(scope_ids=[scope['scope_id']],tab_labels=['Java'])
            text=''.join(c['text'] for c in enrich_context([e],'JDBC连接方法')[0]['contexts'])
            self.assertIn('JAVA_ONLY',text);self.assertNotIn('GO_ONLY',text)

    def test_prompt_deduplicates_same_source_without_dropping_conditions(self):
        e={**row('关键规则'),'contexts':[{'kind':'related','text':'前提\n关键规则\n限制',
          'source_start_line':1,'source_end_line':3,'complete':True},{'kind':'section','text':'前提\n关键规则\n限制',
          'source_start_line':1,'source_end_line':3,'complete':True}]}
        p=messages('问题',[e],'3.x',{'raw_query':'无须重复的长材料','goal':'目标'})[1]['content']
        self.assertEqual(p.count('关键规则'),1)
        self.assertIn('前提',p);self.assertIn('限制',p)
        self.assertNotIn('无须重复的长材料',p)

    def test_context_version_declaration_is_not_summarized_as_absent(self):
        e={**row('模式规则'), 'version_note':[], 'applicability_conditions':[],
           'contexts':[{'kind':'related','text':'## 例外\n自 3.1.0 版本起，由其他属性控制。\n',
                        'source_start_line':40,'source_end_line':41,'complete':True}]}
        prompt=messages('规则是什么？',[e],'3.x')[1]['content']
        self.assertIn('片段条件及版本声明原文：补取原文第 41 行：自 3.1.0 版本起，由其他属性控制。',prompt)
        self.assertNotIn('片段条件及版本声明原文：未显式声明',prompt)

    def test_authorized_service_fallback_does_not_mix_credentials(self):
        import os
        with patch("doris_rag.generation.load_dotenv"), patch.dict(os.environ, {
                "AGENT_MODEL_API_KEY": "fixture", "AGENT_MODEL_BASE_URL": "fixture-host", "AGENT_MODEL_NAME": "fixture-model"}, clear=True):
            self.assertEqual(llm_config()["model"], "fixture-model")
            os.environ["LLM_BASE_URL"] = "different-host"
            self.assertEqual(llm_config()["api_key"], "")

    def source_fixture(self, temp):
        from doris_rag.documents import DOCS_PATH
        path = Path(temp) / DOCS_PATH / "version-3.x/source.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("## 参数\n条件说明\n```sql\nSELECT 1;\n```\n结果为1\n", encoding="utf-8")
        return {**row("SELECT 1;"), "original_path": str(path), "source_sha256": sha256_file(path),
                "source_start_line": 3, "source_end_line": 5, "parent_section_id": "s1",
                "related_context": {"source_start_line": 2, "source_end_line": 6},
                "section_context": {"source_start_line": 1, "source_end_line": 6}}

    def test_original_code_explanation_and_result_are_fetched(self):
        with tempfile.TemporaryDirectory() as temp, patch("doris_rag.context.config", return_value={"source_root": temp}):
            enriched = enrich_context([self.source_fixture(temp)], "完整步骤")[0]
            self.assertEqual(enriched["context_status"], "complete")
            self.assertIn("条件说明", enriched["contexts"][0]["text"])
            self.assertIn("结果为1", enriched["contexts"][0]["text"])

    def test_context_budget_is_explicit_and_not_fake_complete(self):
        with tempfile.TemporaryDirectory() as temp, patch("doris_rag.context.config", return_value={"source_root": temp}):
            enriched = enrich_context([self.source_fixture(temp)], "完整步骤", max_chars=12)[0]
            self.assertEqual(enriched["context_status"], "partial")
            self.assertTrue(enriched["context_issues"])
            self.assertLessEqual(sum(len(c["text"]) for c in enriched["contexts"]), 12)

    def test_source_changed_or_outside_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch("doris_rag.context.config", return_value={"source_root": temp}):
            fixture = self.source_fixture(temp)
            Path(fixture["original_path"]).write_text("changed", encoding="utf-8")
            self.assertEqual(enrich_context([fixture], "条件")[0]["context_status"], "source_changed")
            fixture["original_path"] = str(Path(temp).parent / "elsewhere.md")
            self.assertEqual(enrich_context([fixture], "条件")[0]["context_status"], "untrusted_source")

    def test_conditions_raw_locations_and_fact_policy_reach_answer(self):
        evidence = {**row("规则"), "version_note": ["自3.0.2起支持"], "source_start_line": 88,
                    "source_end_line": 90, "contexts": [{"kind": "related", "source_start_line": 86,
                    "source_end_line": 90, "complete": False, "text": "相关规则"}]}
        prompt = messages("问题", [evidence], "3.x", {"facts": [{"quote": "用户报告"}]})
        self.assertIn("不能转写成用户事实", prompt[0]["content"])
        self.assertIn("自3.0.2起支持", prompt[1]["content"])
        self.assertIn("部分，不能视为完整结构", prompt[1]["content"])
        self.assertIn("用户报告", prompt[1]["content"])

    def test_source_code_errors_and_rule_exceptions_are_not_rewritten_in_evidence(self):
        # 约束适用于任何API/状态例外；原始证据不能被提示构建静默修正。
        source = 'PreparedStatement p; p.setString("[1,2]"); CANCELLED可复用。已移除接口返回404。'
        prompt = messages('如何操作？', [row(source)], '3.x')
        self.assertIn(source, prompt[1]['content'])
        self.assertIn('通用API的必需实参', prompt[0]['content'])
        self.assertIn('完整方法签名', prompt[0]['content'])
        self.assertIn('独立的排除性证据', prompt[0]['content'])
        self.assertIn('状态条件及例外', prompt[0]['content'])
        self.assertIn('不等于该现象只能由此导致', prompt[0]['content'])
        self.assertNotIn('setString(1,', prompt[1]['content'])

    def test_lightweight_checks_are_not_semantic_acceptance(self):
        text = "【用户事实】无。【文档规则】返回1 [证据 1]。【基于证据的推论】无。【未知信息】无。"
        with patch("doris_rag.generation.llm_config", return_value={"api_key": "test", "base_url": "test", "model": "test"}), patch(
                "doris_rag.generation.chat_completion", return_value=text):
            result = answer("PI?", [row("规则")], "3.x")
        self.assertTrue(result["citations_valid"])
        self.assertTrue(result["answer_structure_valid"])
        self.assertEqual(result["groundedness"], "requires_human_review")

    def test_chinese_citation_brackets_are_normalized(self):
        with patch("doris_rag.generation.llm_config", return_value={"model": "test"}), patch(
                "doris_rag.generation.chat_completion", return_value="【用户事实】无\n【文档规则】规则【证据 1】\n【基于证据的推论】无\n【未知信息】无"):
            result = answer("问题", [row("规则")], "3.x")
        self.assertTrue(result["citations_valid"])
        self.assertIn("[证据 1]", result["answer"])

    def test_provider_truncation_is_not_presented_as_generated_answer(self):
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "LEAKED_PARTIAL_MODEL_TEXT"}}]}
        with patch("doris_rag.generation.llm_config", return_value={"model": "test", "api_key": "fixture", "base_url": "https://example.invalid"}), patch(
                "doris_rag.generation.requests.post", return_value=response):
            result = answer("问题", [row("规则")], "3.x")
        self.assertEqual(result["status"], "generation_incomplete")
        self.assertNotIn("LEAKED_PARTIAL_MODEL_TEXT", result["answer"])

    def test_user_fact_section_uses_only_grounded_input_quotes(self):
        text = "【用户事实】已确认这是集群故障。\n【文档规则】规则[证据 1]\n【基于证据的推论】无\n【未知信息】无"
        with patch("doris_rag.generation.llm_config", return_value={"model": "test"}), patch(
                "doris_rag.generation.chat_completion", return_value=text):
            result = answer("用户报告失败", [row("规则")], "3.x", {"facts": [{"quote": "用户报告失败"}], "conditions": []})
        self.assertNotIn("已确认这是集群故障", result["answer"])
        self.assertNotIn("【用户事实】", result["answer"])

    def test_small_function_parent_includes_example_result(self):
        from doris_rag.documents import DOCS_PATH, load_document
        from doris_rag.chunking import split_document
        with tempfile.TemporaryDirectory() as temp, patch("doris_rag.context.config", return_value={"source_root": temp}):
            path = Path(temp) / DOCS_PATH / "version-3.x/pi.md"
            path.parent.mkdir(parents=True)
            path.write_text("---\ntitle: PI\n---\n## 描述\n返回常量。\n## 示例\n```sql\nSELECT PI();\n```\n结果：3.141592653589793", encoding="utf-8")
            doc = load_document(path, "3.x", Path(temp))
            rows = split_document(doc, CharacterTokenizer(), 200)
            evidence = enrich_context([{**rows[0], "rank": 1}], "PI返回什么？")[0]
            self.assertTrue(any("3.141592653589793" in c["text"] for c in evidence["contexts"]))

    def test_shared_pipeline_passes_understanding_to_answer(self):
        retrieval = Mock(manifest={"build_id": "fixture"})
        evidence = {**row("规则"), "retrieval_trace": [{"subquery_id": "q1"}]}
        retrieval.search_plan.return_value = [evidence]
        evidence["contexts"] = []
        with patch("doris_rag.pipeline.enrich_context", side_effect=lambda rows, query: rows), patch(
                "doris_rag.pipeline.answer", return_value={"status": "generated", "answer": "测试"}) as generate:
            result = run_query("Doris3.x 的 PI()", retriever=retrieval)
        self.assertEqual(result["source_version"], "3.x")
        self.assertEqual(generate.call_args.args[3]["versions"], ["3.x"])


if __name__ == "__main__":
    unittest.main()
