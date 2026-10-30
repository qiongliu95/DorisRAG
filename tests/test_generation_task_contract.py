"""生成最后一跳契约；模型均为夹具，不能作为语义质量通过证据。"""
import copy,json,tempfile,unittest
from pathlib import Path
from time import perf_counter
from unittest.mock import patch,Mock
from doris_rag import generation,telemetry,web
from doris_rag.sessions import prepare_turn,finish_state,SessionStore,turn_task
from doris_rag.query_understanding import understand_query
from test_query_pipeline import row

QUESTION='列转行和聚合多维分析有什么区别'
def plan(turn):
    return understand_query(turn['query'],turn['version'],turn.get('analysis_seed'),
        version_scope=turn.get('version_scope'),resolved_scope=turn.get('resolved_scope'),task=turn_task(turn))
def state():
    t=prepare_turn({},QUESTION)
    return finish_state(t['state'],{'status':'generated','answer':'fixture','understanding':plan(t),'searched_versions':['4.x']})
def evidence():
    return [{**row('列转行与聚合的独立证据。','3.x'), 'contexts':[]}]

class GenerationTaskContractTests(unittest.TestCase):
    def projected(self,turn):
        p=plan(turn);scope=','.join(p['versions'])
        built=generation.messages(turn['query'],evidence(),scope,p)
        request=generation.answer_request(turn['query'],scope,p)
        generation.validate_answer_projection(built,request)
        actual,_=json.JSONDecoder().raw_decode(built[-1]['content'].split('：',1)[1])
        self.assertEqual(actual,request)
        return p,request,built

    def test_current_request_is_not_old_topic_and_scope_is_exact(self):
        t=prepare_turn(state(),'其他所有版本区别');before=copy.deepcopy(t)
        p,r,built=self.projected(t)
        self.assertEqual(r['latest_request'],'其他所有版本区别')
        self.assertEqual(r['topic_goal'],QUESTION)
        self.assertEqual(r['comparison_objects'],['列转行','聚合多维分析'])
        self.assertEqual(r['requested_scope'],['3.x','2.1'])
        self.assertEqual(r['answer_operation'],'compare_versions')
        self.assertNotIn('4.x',json.dumps(r))
        self.assertIn('没有发现已记载差异时明确说明',built[0]['content'])
        self.assertEqual(t,before)

    def test_single_turn_and_explicit_topic_switch(self):
        for t in [prepare_turn({},'Doris 3.x PI() 返回什么？'),prepare_turn(state(),'换个问题，Doris 2.1 ABS() 怎么用？')]:
            p,r,b=self.projected(t)
            self.assertEqual(r['latest_request'],t['task']['raw_query'])
            self.assertEqual(r['answer_operation'],'answer_latest_request')
            self.assertNotIn('列转行',r['topic_goal'])
            self.assertEqual(r['requested_scope'],p['versions'])

    def test_relative_version_comparison_is_not_subject_hardcoding(self):
        for question in ['Alpha和Beta有什么区别','索引A和索引B有什么区别']:
            t=prepare_turn({},question);s=finish_state(t['state'],{'status':'generated','answer':'x','understanding':plan(t),'searched_versions':['4.x']})
            _,r,_=self.projected(prepare_turn(s,'其他所有版本区别'))
            self.assertEqual(r['answer_operation'],'compare_versions')
            self.assertEqual(r['topic_goal'],question)

    def test_legacy_session_and_frozen_retry_do_not_reprepare(self):
        old=state();old.pop('comparison_objects');old.pop('resolved_scope',None)
        self.projected(prepare_turn(old,'其他所有版本区别'))
        with tempfile.TemporaryDirectory() as d:
            db=SessionStore(Path(d)/'s.db');doc=db.create()
            db.accept(doc['id'],QUESTION);doc=db.get(doc['id']);mid=doc['messages'][-1]['id'];frozen=copy.deepcopy(doc['retry_turn'])
            db.finish(doc['id'],mid,{'status':'execution_failed','answer':'fixture','evidence':[]})
            doc=db.get(doc['id'])
            # 模拟旧快照：消费端由已有turn_task补任务，不改冻结query。
            with db.connect() as c:
                raw=db._read(c,doc['id']);raw['retry_turn'].pop('task');raw['retry_turn'].pop('resolved_scope',None);db._save(c,raw)
            with patch('doris_rag.sessions.prepare_turn',side_effect=AssertionError('retry must not reprepare')):
                doc,_,t=db.retry(doc['id'],mid,doc['revision'])
            self.assertEqual(t['query'],frozen['query']);self.assertEqual(len(doc['messages']),2)
            p=plan(t);r=generation.answer_request(t['query'],','.join(p['versions']),p)
            self.assertEqual(r['latest_request'],QUESTION)
            self.assertNotEqual(t['attempt_id'],frozen['attempt_id'])

    def test_missing_or_invalid_modern_fields_block_before_model(self):
        t=prepare_turn(state(),'其他所有版本区别');p=plan(t)
        for key in ['raw_query','goal','comparison_objects','versions']:
            bad=copy.deepcopy(p);bad.pop(key)
            with self.subTest(key=key),patch.object(generation,'llm_config',return_value={'model':'fixture'}),patch.object(generation,'chat_completion') as llm:
                with self.assertRaises(generation.GenerationContractError):generation.answer(t['query'],evidence(),'3.x,2.1',bad)
                llm.assert_not_called()
        for key,value in [('raw_query',''),('goal',None),('comparison_objects','wrong'),('versions',[])]:
            bad={**p,key:value}
            with self.subTest(key=key),self.assertRaises(generation.GenerationContractError):generation.messages(t['query'],evidence(),'3.x,2.1',bad)

    def test_wrong_scope_or_material_and_omitted_projection_block(self):
        t=prepare_turn(state(),'其他所有版本区别');p=plan(t)
        with self.assertRaises(generation.GenerationContractError):generation.messages(t['query'],evidence(),'4.x',p)
        with self.assertRaises(generation.GenerationContractError):generation.messages('other material',evidence(),'3.x,2.1',p)
        with patch.object(generation,'llm_config',return_value={'model':'fixture'}),patch.object(generation,'messages',return_value=[{'role':'user','content':'old topic only'}]),patch.object(generation,'chat_completion') as llm:
            with self.assertRaises(generation.GenerationContractError):generation.answer(t['query'],evidence(),'3.x,2.1',p)
            llm.assert_not_called()

    def test_tampered_task_is_not_silently_accepted(self):
        t=prepare_turn(state(),'其他所有版本区别');p,r,b=self.projected(t)
        for field,value in [('latest_request',QUESTION),('requested_scope',['4.x']),('comparison_objects',['wrong'])]:
            wrong={**r,field:value};bad=copy.deepcopy(b);bad[-1]['content']='本轮回答任务(JSON)：'+json.dumps(wrong)+'\n\nbackground'
            with self.subTest(field=field),self.assertRaises(generation.GenerationContractError):generation.validate_answer_projection(bad,r)

    def test_legacy_call_uses_query_instead_of_inventing_latest_from_goal(self):
        built=generation.messages('本轮原问',evidence(),'3.x',{'raw_query':'旧语义长材料','goal':'旧主题'})
        r,_=json.JSONDecoder().raw_decode(built[-1]['content'].split('：',1)[1])
        self.assertEqual(r['latest_request'],'本轮原问');self.assertEqual(r['input_contract'],'legacy_query')
        self.assertNotIn('旧语义长材料',built[-1]['content'])
