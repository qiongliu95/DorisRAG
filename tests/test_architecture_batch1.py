"""任务契约与分离文档覆盖；不以同一片段含全部对象掩盖接口污染。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from doris_rag.sessions import prepare_turn,finish_state,SessionStore,turn_task
from doris_rag.query_understanding import understand_query
from doris_rag.retrieval import comparison_objects
from doris_rag.pipeline import run_query
from doris_rag import generation,telemetry
from test_query_pipeline import fake_retriever,row

QUESTION='列转行和聚合多维分析有什么区别'
def plan(turn):
    return understand_query(turn['query'],turn['version'],turn['analysis_seed'],
        version_scope=turn['version_scope'],resolved_scope=turn.get('resolved_scope'),task=turn_task(turn))
def initial(question=QUESTION):
    t=prepare_turn({},question)
    return finish_state(t['state'],{'status':'generated','answer':'fixture','understanding':plan(t),'searched_versions':['4.x']})

class TaskContractTests(unittest.TestCase):
    def test_distinct_original_goal_material_and_queries(self):
        t=prepare_turn(initial(),'其他所有版本区别');p=plan(t)
        self.assertEqual(p['raw_query'],'其他所有版本区别')
        self.assertEqual(p['goal'],QUESTION)
        self.assertEqual(p['material_query'],QUESTION+'\n\n其他所有版本区别')
        self.assertEqual(t['query'].count(QUESTION),1)
        self.assertNotIn('当前目标：',t['query'])
        self.assertEqual(comparison_objects(p),['列转行','聚合多维分析'])
        self.assertEqual(p['versions'],['3.x','2.1'])
        self.assertTrue(p['scope_reused'])

    def test_objects_updated_only_with_task_not_scope_or_model_summary(self):
        state=initial('Alpha和Beta有什么区别')
        for text in ('其他所有版本区别','那其他版本呢','2.1 和 3.x 有什么不同','所有版本都一样吗'):
            self.assertEqual(plan(prepare_turn(state,text))['comparison_objects'],['Alpha','Beta'])
        corrected=prepare_turn(state,'不是Beta，而是Gamma')
        self.assertEqual(plan(corrected)['comparison_objects'],['Alpha','Gamma'])
        focus=prepare_turn(state,'那在其他版本找一下是否有Alpha的相关信息')
        self.assertEqual(plan(focus)['comparison_objects'],[])
        switched=prepare_turn(state,'换个话题，Iceberg Catalog 有什么变化')
        self.assertEqual(plan(switched)['comparison_objects'],[])
        self.assertNotIn('Alpha',switched['query'])
        p=plan(prepare_turn(state,'其他版本区别'));p['goal']='包装文本Alpha和Wrong的区别'
        self.assertEqual(comparison_objects(p),['Alpha','Beta'])

    def test_scope_passed_without_text_roundtrip_and_keeps_version_switch(self):
        state=initial()
        for text,versions in [('其他所有版本区别',['3.x','2.1']),('所有版本都一样吗',['4.x','3.x','2.1']),('那在Doris 2.1查一下',['2.1'])]:
            t=prepare_turn(state,text)
            self.assertEqual(t['resolved_scope']['versions'],versions)
            # 非范围材料的事实识别仍可读取版本；范围解析器不应再处理本轮范围文本。
            with patch('doris_rag.query_understanding._set_version_scope',side_effect=AssertionError('no scope re-resolution')):
                self.assertEqual(plan(t)['versions'],versions)
        switched=prepare_turn(prepare_turn({},'Doris 3.0.2 的 PI() 返回什么')['state'],'版本改为2.1.4')
        self.assertEqual(plan(switched)['versions'],['2.1'])

    def test_legacy_state_and_retry_snapshot_compatible(self):
        old=initial();old.pop('comparison_objects');old.pop('resolved_scope')
        t=prepare_turn(old,'其他所有版本区别')
        self.assertEqual(plan(t)['comparison_objects'],['列转行','聚合多维分析'])
        legacy=copy.deepcopy(t);legacy.pop('task');legacy.pop('resolved_scope')
        frozen=copy.deepcopy(legacy)
        self.assertEqual(turn_task(legacy)['goal'],QUESTION)
        self.assertEqual(legacy,frozen)
        with tempfile.TemporaryDirectory() as tmp:
            db=SessionStore(Path(tmp)/'s.db');d=db.create();_,mid,t=db.accept(d['id'],QUESTION)
            db.finish(d['id'],mid,{'status':'execution_failed','answer':'fixture','evidence':[]})
            _,same,retry=db.retry(d['id'],mid,db.get(d['id'])['revision'])
            self.assertEqual(mid,same)
            for key in ('query','fact_sources','task','resolved_scope'):
                self.assertEqual(retry[key],t[key])

    def test_confirmed_current_history_scope_not_reopened(self):
        scope={'versions':['2.1'],'version_policy':'resolved_query_targets',
            'version_mentions':[{'literal':'2.0.0','family':'2.0','supported_source':False},
                                {'literal':'2.1.2','family':'2.1','supported_source':True}],
            'missing_version_sources':[],'requested_version_literals':['2.1.2']}
        with patch('doris_rag.query_understanding.chat_completion',side_effect=AssertionError('no extra model')):
            p=understand_query('PI() 返回什么',version_scope='之前2.0.0，现在2.1.2',resolved_scope=scope)
        self.assertEqual(p['status'],'ready')
        self.assertEqual(p['versions'],['2.1'])

    def test_distinct_documents_reach_model_messages(self):
        for question,objects in [(QUESTION,['列转行','聚合多维分析']),('Alpha和Beta有什么区别',['Alpha','Beta'])]:
            t=prepare_turn(initial(question),'其他所有版本区别')
            rows=[]
            for v in ('3.x','2.1'):
                for n in range(6):
                    subject=objects[n==5]
                    r=row(subject+'的独立语法和适用条件。',v,len(rows))
                    r.update(doc_id=subject+v,title=subject,section_path=subject,contexts=[])
                    rows.append(r)
            engine=fake_retriever(rows)
            def ranked(q,v,*args):
                return [{'index':i,'score':1/(n+1),'rank':n+1,'channel_hits':[]} for n,i in enumerate(i for i,r in enumerate(rows) if r['source_version']==v)]
            engine._rank=Mock(side_effect=ranked)
            with telemetry.request_trace(True,persist=False) as trace,patch('doris_rag.pipeline.enrich_context',side_effect=lambda rs,q:rs),patch.object(generation,'llm_config',return_value={'model':'fixture','base_url':'fixture','api_key':'fixture'}),patch.object(generation,'chat_completion',return_value='依据两对象文档。[证据 1][证据 2]'):
                result=run_query(t['query'],t['version'],resolved_scope=t['resolved_scope'],task=t['task'],retriever=engine)
            sent=next(e['data'] for e in trace.data['events'] if e['name']=='generation.input_rows')
            built=generation.messages(t['query'],sent,','.join(result['understanding']['versions']),result['understanding'])
            for obj in objects:
                self.assertTrue(any(r['title']==obj for r in sent))
                self.assertIn(obj+'的独立语法',built[-1]['content'])
            self.assertEqual(result['understanding']['comparison_objects'],objects)
