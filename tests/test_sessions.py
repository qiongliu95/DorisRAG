import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from doris_rag import web
from doris_rag.sessions import SessionStore, SessionBusy, prepare_turn, finish_state, resolve_turn
from doris_rag.query_understanding import understand_query
from doris_rag.pipeline import run_query


def scope(turn):
    return understand_query(turn['query'], turn['version'], version_scope=turn.get('version_scope'))


def independent_turn(previous, text):
    # 新的异步关系边界需经过理解；保留原有最终任务、原文及范围断言。
    turn = prepare_turn(previous, text)
    with patch('doris_rag.query_understanding.chat_completion', return_value=json.dumps(
            {'relation':'new_question', 'clarification':''})):
        return resolve_turn(turn)


class ConversationRules(unittest.TestCase):
    def test_new_comparison_has_no_fabricated_history_or_duplicate_question(self):
        text='比较 Doris 2.1 和3.x 的 NGram BloomFilter 使用条件。文档示例从7.60秒到0.93秒，是否能保证我的查询也快8倍？'
        turn=prepare_turn({},text)
        self.assertEqual(turn['query'],text)
        self.assertEqual(scope(turn)['versions'],['2.1','3.x'])
        self.assertEqual(scope(turn)['kind'],'knowledge')

    def test_new_scenario_with_change_in_description_is_not_old_correction(self):
        first=prepare_turn({},'PI() 返回什么？')
        nxt=prepare_turn(first['state'],'Doris 3.1.4-rc02，dynamic_partition.enable=false，改为true后命名不同，如何解释？')
        self.assertEqual(nxt['kind'],'new_question')
        self.assertNotIn('PI()',nxt['query'])
        self.assertEqual(nxt['state']['environment_version'],'3.x')

    def test_latest_default_and_explicit_only(self):
        for q, expected in [('PI()返回什么？',['4.x']),('Doris 2.1 PI()',['2.1']),
                            ('比较 Doris 3.x 和4.x的PI()',['3.x','4.x']),
                            ('PI()请查所有版本',['4.x','3.x','2.1']),
                            ('PI()请跨版本搜索',['4.x','3.x','2.1'])]:
            self.assertEqual(scope(prepare_turn({},q))['versions'],expected)

    def test_multiple_without_comparison_asks_one_question(self):
        self.assertEqual(understand_query('Doris 3.x 4.x PI()')['status'],'needs_clarification')

    def test_short_follow_up_keeps_goal_and_version(self):
        first=prepare_turn({},'Doris3.0.2 的 hive.recursive_directories 有什么作用？')
        nxt=prepare_turn(first['state'],'那具体怎么配置？')
        self.assertEqual(nxt['kind'],'follow_up')
        self.assertIn('hive.recursive_directories',nxt['query'])
        self.assertEqual(scope(nxt)['versions'],['3.x'])

    def test_version_comparison_without_prefix_preserves_subject_and_temporary_scope(self):
        question = 'Doris 4.0.2 中 hive.recursive_directories 有什么作用和使用条件？'
        first = prepare_turn({}, question)
        initial = understand_query(first['query'], first['version'])
        previous = finish_state(first['state'], {'status': 'generated', 'answer': 'fixture', 'understanding': initial})
        for text, versions in [('所有版本区别在哪', ['4.x', '3.x', '2.1']),
                               ('3.x和4.x区别在哪', ['3.x', '4.x']),
                               ('全部版本有什么不同？', ['4.x', '3.x', '2.1'])]:
            with self.subTest(text=text):
                turn = prepare_turn(previous, text)
                plan = understand_query(turn['query'], turn['version'], turn['analysis_seed'], version_scope=turn.get('version_scope'))
                self.assertEqual(turn['kind'], 'follow_up')
                self.assertIn('hive.recursive_directories', turn['query'])
                self.assertIn(text, turn['query'])
                self.assertEqual(plan['versions'], versions)
                self.assertIn('hive.recursive_directories', plan['technical_literals'])
                self.assertTrue(all('hive.recursive_directories' in q['text'] for q in plan['subqueries']))
                self.assertEqual(turn['state']['goal'], question)
                self.assertTrue(turn['state']['temporary_scope'])
                self.assertEqual(turn['state']['environment_literal'], '4.0.2')
                self.assertIsNone(turn['analysis_seed'])
                after = finish_state(turn['state'], {'status': 'generated', 'answer': 'fixture', 'understanding': plan})
                self.assertEqual(after['last_understanding'], previous['last_understanding'])
                follow = prepare_turn(after, '那具体怎么配置？')
                self.assertIn('hive.recursive_directories', follow['query'])
                self.assertEqual(scope(follow)['versions'], ['4.x'])
        self.assertEqual(previous['inputs'], [question])

    def test_version_comparison_new_objects_and_missing_history_do_not_inherit_subject(self):
        previous = prepare_turn({}, 'Doris 4.0.2 中 hive.recursive_directories 有什么作用？')['state']
        for text, kind, obj in [('换个问题，所有版本的 PI() 返回什么？', 'topic_switch', 'PI'),
                                ('所有版本的 ABS() 有什么区别？', 'new_question', 'ABS'),
                                ('3.x和4.x的 Routine Load 有什么区别？', 'new_question', 'Routine Load')]:
            with self.subTest(text=text):
                turn = independent_turn(previous, text)
                plan = scope(turn)
                self.assertEqual(turn['kind'], kind)
                self.assertEqual(turn['query'], text)
                self.assertNotIn('hive.recursive_directories', turn['query'])
                self.assertIn(obj, plan['subqueries'][0]['text'])
                self.assertEqual(turn['state']['goal'], text)
                self.assertEqual(turn['state']['raw_inputs'], [text])
                self.assertFalse(turn['state']['temporary_scope'])
                self.assertEqual(plan['versions'], ['3.x', '4.x'] if text.startswith('3.x') else ['4.x', '3.x', '2.1'])
        for text in ('所有版本区别在哪', '3.x和4.x区别在哪'):
            with self.subTest(no_history=text):
                turn = prepare_turn({}, text)
                plan = scope(turn)
                self.assertEqual(turn['kind'], 'new_question')
                self.assertEqual(turn['query'], text)
                self.assertEqual(turn['state']['goal'], text)
                self.assertFalse(turn['state']['temporary_scope'])
                self.assertEqual(plan['technical_literals'], [])
                self.assertEqual(plan['versions'], ['4.x', '3.x', '2.1'] if text.startswith('所有') else ['3.x', '4.x'])

    def test_further_failure_material_keeps_task_but_new_question_drops_it(self):
        first=prepare_turn({},'Doris 3.x 的 hive.recursive_directories 有什么作用？')
        for text in ('还是不行，新的日志是 timeout','我按照你说的设置后仍然失败'):
            turn=prepare_turn(first['state'],text)
            self.assertEqual(turn['kind'],'supplement')
            self.assertIn('hive.recursive_directories',turn['query'])
        turn=prepare_turn(first['state'],'再问一个问题，PI()返回什么？')
        self.assertNotIn('hive.recursive_directories',turn['query'])
        self.assertEqual(scope(turn)['versions'],['4.x'])

    def test_version_correction_replaces_old_condition_and_evidence(self):
        first=prepare_turn({},'Doris3.0.2 的 PI() 有什么作用？')
        first['state']['used_evidence']=[{'source_version':'3.x'}]
        nxt=prepare_turn(first['state'],'纠正一下，是 Doris 2.1.4')
        # 版本纠正更新独立范围；旧问题是原始材料，不再被替换。
        self.assertIn(first['state']['goal'],nxt['query'])
        self.assertEqual(scope(nxt)['versions'],['2.1'])
        self.assertEqual(nxt['state']['used_evidence'],[])
        self.assertEqual(nxt['state']['environment_literal'],'2.1.4')

    def test_same_family_patch_correction(self):
        first=prepare_turn({},'Doris 4.1.3 的 Upload Action 是什么？')
        nxt=prepare_turn(first['state'],'版本是4.1.4')
        self.assertEqual(nxt['kind'],'supplement')
        self.assertIn(first['state']['goal'],nxt['query'])
        self.assertIn('4.1.4',nxt['query'])
        self.assertEqual(scope(nxt)['version_mentions'][0]['literal'],'4.1.4')

    def test_config_correction_removes_old_value(self):
        first=prepare_turn({},'我的配置 enable_insert_strict=false，为什么导入失败？')
        nxt=prepare_turn(first['state'],'纠正，enable_insert_strict=true')
        self.assertNotIn('enable_insert_strict=false',nxt['query'])
        self.assertIn('enable_insert_strict=true',nxt['query'])

    def test_temporary_comparison_does_not_change_environment(self):
        first=prepare_turn({},'Doris 3.0.2 的 PI() 有什么作用？')
        comparison=prepare_turn(first['state'],'那比较 2.1 和4.x 的 PI() 返回值')
        self.assertEqual(set(scope(comparison)['versions']),{'2.1','4.x'})
        self.assertEqual(comparison['state']['environment_version'],'3.x')
        nxt=prepare_turn(comparison['state'],'那给一个使用示例？')
        self.assertEqual(scope(nxt)['versions'],['3.x'])

    def test_temporary_comparison_does_not_replace_active_analysis_goal(self):
        first=prepare_turn({},'我用的是 Doris 2.1.4，PI()是什么？')
        original={'status':'ready','goal':'PI()用途','facts':[],'conditions':[], 'technical_literals':['PI'],
                  'subqueries':[{'id':'q1','text':'PI()用途','technical_literals':['PI']}]}
        state=finish_state(first['state'],{'status':'generated','answer':'答案','understanding':original})
        comparison=prepare_turn(state,'那比较3.x和4.x的PI()')
        temporary={**original,'goal':'比较3.x和4.x的PI()',
                   'subqueries':[{'id':'q1','text':'比较3.x和4.x的PI()','technical_literals':['PI']}]}
        state=finish_state(comparison['state'],{'status':'generated','answer':'比较答案','understanding':temporary})
        follow=prepare_turn(state,'那具体含义呢？')
        self.assertEqual(follow['analysis_seed']['goal'],'PI()用途')
        self.assertNotIn('3.x',follow['analysis_seed']['subqueries'][0]['text'])
        self.assertEqual(scope(follow)['versions'],['2.1'])

    def test_reported_version_is_user_fact_without_extra_model(self):
        with patch('doris_rag.query_understanding.chat_completion') as llm:
            plan=understand_query('纠正一下，我用的是 Doris 2.1.4')
        self.assertEqual(plan['facts'][0]['quote'],'我用的是 Doris 2.1.4')
        llm.assert_not_called()

    def test_topic_switch_drops_facts_and_uses_default(self):
        first=prepare_turn({},'Doris 2.1 我的配置 option_a=false')
        for question in ('换个问题，PI()返回什么？','ABS() 的用途是什么？'):
            nxt=independent_turn(first['state'],question)
            self.assertNotIn('option_a',nxt['query'])
            self.assertNotIn('2.1',nxt['query'])
            self.assertEqual(scope(nxt)['versions'],['4.x'])

    def test_necessary_clarification_then_short_reply(self):
        first=prepare_turn({},'帮我看看')
        self.assertTrue(first['clarification'])
        state=finish_state(first['state'],{'status':'needs_clarification','answer':'具体哪个参数？'})
        reply=prepare_turn(state,'enable_insert_strict')
        self.assertEqual(reply['kind'],'clarification_reply')
        self.assertIn('enable_insert_strict',reply['query'])

    def test_model_inference_never_becomes_fact(self):
        state=prepare_turn({},'PI()是什么？')['state']
        state=finish_state(state,{'status':'generated','answer':'【基于证据的推论】可能原因【未知信息】无',
                                 'understanding':{'facts':[]},'evidence':[]})
        nxt=prepare_turn(state,'那举例呢？')
        self.assertNotIn('可能原因',nxt['query'])
        self.assertFalse(nxt['state']['facts'])

    def test_short_follow_up_reuses_analysis_without_llm_or_growing_queries(self):
        state=prepare_turn({},'Doris 3.x 日志如下：\nERROR: upload失败')['state']
        plan={'status':'ready','goal':'Upload Action 用法','facts':[{'quote':'ERROR: upload失败'}],
              'conditions':[],'technical_literals':['Upload'],
              'subqueries':[{'id':'q1','text':'Upload Action 用法','technical_literals':['Upload']}]}
        state=finish_state(state,{'status':'generated','answer':'答案','understanding':plan})
        with patch('doris_rag.query_understanding.chat_completion') as llm:
            for _ in range(4):
                turn=prepare_turn(state,'那具体怎么做？')
                p=understand_query(turn['query'],turn['version'],turn['analysis_seed'],version_scope=turn.get('version_scope'))
                self.assertTrue(p['comprehension_reused'])
                self.assertEqual(p['subqueries'][0]['text'].count('本轮追问'),1)
                state=finish_state(turn['state'],{'status':'generated','answer':'答案','understanding':p})
            llm.assert_not_called()

    def test_unsupported_and_ambiguous_corrections_require_clarification(self):
        first=prepare_turn({},'Doris 3.x PI()是什么？')
        self.assertEqual(scope(prepare_turn(first['state'],'纠正为 Doris 2.0'))['status'],'needs_clarification')
        self.assertTrue(prepare_turn(first['state'],'纠正一下，刚才说错了')['clarification'])

    def test_correction_clarification_reply_replaces_old_value(self):
        first=prepare_turn({},'Doris 3.x，我的配置 enable_insert_strict=false，为什么导入失败？')
        correction=prepare_turn(first['state'],'纠正一下，刚才说错了')
        pending=finish_state(correction['state'],{'status':'needs_clarification','answer':correction['clarification']})
        for legacy in (False,True):
            state=dict(pending)
            if legacy:state.pop('pending_correction',None)
            reply=prepare_turn(state,'enable_insert_strict=true')
            self.assertEqual(reply['kind'],'correction')
            self.assertNotIn('enable_insert_strict=false',reply['query'])
            self.assertIn('enable_insert_strict=true',reply['state']['goal'])
            self.assertEqual(reply['state']['environment_version'],'3.x')
            self.assertFalse(reply['state']['pending_correction'])

    def test_correction_clarification_new_topic_drops_pending_flag(self):
        first=prepare_turn({},'PI()是什么？')
        turn=prepare_turn(first['state'],'纠正一下，刚才说错了')
        state=finish_state(turn['state'],{'status':'needs_clarification','answer':turn['clarification']})
        new=prepare_turn(state,'换个问题，ABS()返回什么？')
        self.assertEqual(new['kind'],'topic_switch')
        self.assertFalse(new['state']['pending_correction'])

    def test_natural_correction_replaces_operation_and_drops_evidence(self):
        first=prepare_turn({},'Broker Load 的使用步骤是什么？')
        turn=prepare_turn(first['state'],'不是 Broker Load，而是 Stream Load')
        self.assertNotIn('Broker Load',turn['state']['goal'])
        self.assertIn('Stream Load',turn['state']['goal'])

    def test_session_material_does_not_create_synthetic_user_fact(self):
        plan={'status':'ready','kind':'knowledge','versions':['4.x'],'facts':[{'quote':'当前目标：合成文本'}],
              'conditions':[],'technical_literals':[],'subqueries':[{'id':'q1'}]}
        with patch('doris_rag.pipeline.understand_query',return_value=plan),patch('doris_rag.pipeline.enrich_context',return_value=[]),patch(
                'doris_rag.pipeline.answer',return_value={'status':'no_evidence','answer':'无'}) as generate:
            retrieval=Mock(manifest={'build_id':'fixture'})
            retrieval.search_plan.return_value=[]
            result=run_query('当前目标：合成文本',retriever=retrieval,fact_sources=['原始用户问题'])
        self.assertEqual(generate.call_args.args[3]['facts'],[])
        self.assertIn('total_seconds',result['timings'])


class SessionPersistence(unittest.TestCase):
    def test_history_reopen_switch_delete_and_restart_interruption(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'sessions.db'; store=SessionStore(path)
            first=store.create(); second=store.create()
            doc,mid,turn=store.accept(first['id'],'PI()是什么？',0)
            with self.assertRaises(SessionBusy):store.accept(first['id'],'重复发送',1)
            recovered=SessionStore(path)
            self.assertEqual(recovered.get(first['id'])['messages'][-1]['status'],'interrupted')
            self.assertFalse(recovered.get(first['id'])['busy'])
            self.assertEqual(len(recovered.list()),2)
            recovered.delete(second['id'])
            with self.assertRaises(KeyError):recovered.get(second['id'])
            self.assertEqual(recovered.get(first['id'])['messages'][0]['content'],'PI()是什么？')

    def test_optimistic_revision_rejects_stale_tab(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(Path(d)/'s.db');doc=store.create()
            doc,mid,_=store.accept(doc['id'],'PI()?',0)
            store.finish(doc['id'],mid,{'status':'generated','answer':'答案'})
            with self.assertRaises(SessionBusy):store.accept(doc['id'],'重复提交',0)

    def test_async_http_roundtrip_and_error_status(self):
        with tempfile.TemporaryDirectory() as d,patch('doris_rag.web.store',SessionStore(Path(d)/'s.db')):
            client=TestClient(web.app);doc=client.post('/api/sessions').json()
            with patch('doris_rag.web.index_ready',return_value=True),patch('doris_rag.web.run_query',return_value={'status':'generated','answer':'fixture','evidence':[],
                        'source_version':'4.x','timings':{'llm_calls':1}}):
                accepted=client.post('/api/sessions/'+doc['id']+'/turns',json={'query':'PI()','revision':0})
                self.assertEqual(accepted.status_code,202)
                for _ in range(100):
                    result=client.get('/api/sessions/'+doc['id']).json()
                    if not result['busy']:break
                    time.sleep(.02)
            self.assertFalse(result['busy']);self.assertEqual(result['messages'][-1]['status'],'generated')
            self.assertEqual(result['messages'][-1]['result']['timings']['llm_calls'],1)
            self.assertEqual(client.post('/api/sessions/'+doc['id']+'/turns',json={'query':'repeat','revision':0}).status_code,409)
            self.assertEqual(client.delete('/api/sessions/'+doc['id']).status_code,204)
            self.assertEqual(client.get('/api/sessions/'+doc['id']).status_code,404)


if __name__=='__main__':unittest.main()
