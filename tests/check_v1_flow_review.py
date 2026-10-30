"""不依赖 Dense 的正式网页流程核查；不调用 LLM，仅自身会话及标明的答案回放。"""
import argparse
import copy
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/v1_flow_review_20261024'
BASE='http://127.0.0.1:8765'


def run(tag):
    checks={};created=[];errors=[];api_results=[]
    with sync_playwright() as p:
        http=p.request.new_context(base_url=BASE)
        browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
        context=browser.new_context(viewport={'width':1366,'height':900})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(BASE,wait_until='networkidle');page.wait_for_selector('.guide')
            a=page.evaluate('current.id');created.append(a)
            checks['home_three_guides']=page.locator('.guide').count()==3
            for guide in page.locator('.guide').all():
                guide.click();checks['guide_editable_no_auto_send']=bool(page.locator('#query').input_value()) and page.locator('.message').count()==0
            held=[];turn_url='**/api/sessions/'+a+'/turns'
            page.route(turn_url,lambda route:held.append(route))
            page.locator('#query').fill('帮我看看');page.locator('#send').click()
            page.wait_for_function('sending===true')
            for _ in range(30):
                if held:break
                page.wait_for_timeout(50)
            assert held,'发送请求没有被测试控制器捕获'
            draft='下一轮补充：请保留尚未发送的草稿。'
            page.locator('#query').fill(draft)
            held[0].continue_();page.unroute(turn_url)
            page.wait_for_function('current.messages.length===2 && !current.busy && !sending',timeout=30000)
            checks['draft_survives_inflight_ack']=page.locator('#query').input_value()==draft
            checks['necessary_clarification']=page.locator('.status-tag').last.inner_text()=='需要澄清'
            page.reload(wait_until='networkidle');page.wait_for_selector('.message')
            checks['refresh_restores_session']=page.evaluate('current.id')==a
            checks['refresh_restores_unsent_draft']=page.locator('#query').input_value()==draft

            polls=[0]
            def transient(route):
                polls[0]+=1
                if polls[0]==1:route.fulfill(status=503,json={'detail':'测试：短暂断开'})
                else:route.continue_()
            page.route('**/api/sessions/'+a,transient)
            page.locator('#query').fill('是');page.locator('#send').click()
            page.wait_for_function("!document.getElementById('error').hidden",timeout=10000)
            checks['temporary_disconnect_visible']='恢复连接' in page.locator('#error').inner_text()
            page.wait_for_function('current.messages.length===4 && !current.busy',timeout=30000)
            checks['recovered_connection_clears_stale_error']=page.locator('#error').is_hidden()
            page.unroute('**/api/sessions/'+a)

            rejected=[0]
            def reject(route):
                rejected[0]+=1;route.fulfill(status=503,json={'detail':'测试：提交未执行'})
            page.route(turn_url,reject)
            page.locator('#query').fill('应保留的失败输入');page.locator('#send').click()
            page.wait_for_function('!sending')
            checks['failed_submit_keeps_input']=page.locator('#query').input_value()=='应保留的失败输入'
            checks['failed_submit_not_success']=page.locator('.message').count()==4 and page.locator('#error').is_visible()
            checks['failed_submit_not_auto_replayed']=rejected[0]==1
            page.unroute(turn_url)

            page.locator('#new-session').click();page.wait_for_selector('.guide')
            b=page.evaluate('current.id');created.append(b)
            checks['new_session_empty']=b!=a and page.locator('.message').count()==0
            page.locator('#query').fill('B会话草稿')
            page.locator('[data-session-id="'+a+'"] .history-item').click();page.wait_for_selector('.message')
            checks['switch_history']=page.evaluate('current.id')==a
            page.locator('[data-session-id="'+b+'"] .history-item').click();page.wait_for_selector('.guide')
            checks['session_drafts_isolated']=page.locator('#query').input_value()=='B会话草稿'
            page.locator('[data-session-id="'+a+'"] .history-item').click();page.wait_for_selector('.message')

            # 以下内容仅回放已有真实组件结果，验证展示；不伪造生产问答成功。
            captured=[json.loads(line) for line in (ROOT/'reports/v1_integration_20261024/component_cases.jsonl').read_text(encoding='utf-8').splitlines()]
            good=next(r['result'] for r in captured if r['result']['status']=='generated')
            insufficient=next(r['result'] for r in captured if r['result']['status']=='insufficient_evidence')
            doc=http.get('/api/sessions/'+a).json();doc['busy']=False;doc['messages']=[]
            for result in [good,insufficient,{'status':'generation_incomplete','answer':'模型输出不完整，未交付半截答案。'},
                           {'status':'execution_failed','answer':'处理未完成，请重试。'},
                           {'status':'source_integrity_failed','answer':'证据原文校验失败，未生成答案。'}]:
                doc['messages'].append({'role':'assistant','status':result['status'],'content':result['answer'],'result':result})
            page.evaluate('(doc)=>setCurrent(doc)',doc)
            tags=page.locator('.status-tag').all_inner_texts()
            checks['answer_and_failure_states_replay']=tags==['完成','证据不足','答案不完整','执行失败','来源校验失败']
            page.locator('.sources').first.locator('summary').first.click()
            checks['evidence_versions_replay']='Doris 4.x' in page.locator('.sources').first.inner_text()
            checks['persistent_version_hint']=page.locator('#version-hint').is_visible() and '默认仅查询' in page.locator('#version-hint').inner_text()
            page.screenshot(path=str(OUT/(tag+'_states.png')),full_page=True)
            page.once('dialog',lambda dialog:dialog.dismiss());page.locator('[data-session-id="'+b+'"] .delete').click()
            checks['delete_cancel_keeps_history']=http.get('/api/sessions/'+b).status==200
            page.once('dialog',lambda dialog:dialog.accept());page.locator('[data-session-id="'+b+'"] .delete').click()
            page.wait_for_function('(sid)=>!document.querySelector(`[data-session-id="${sid}"]`)',arg=b)
            checks['delete_own_test_session']=http.get('/api/sessions/'+b).status==404

            # 真实 HTTP 保存/更新状态；索引未就绪时不触发任何分析或生成。
            if http.get('/api/status').json()['index_ready']:raise RuntimeError('索引已就绪，本脚本不发起模型测试')
            c=http.post('/api/sessions').json();created.append(c['id'])
            texts=['Doris 3.x，我的配置 enable_insert_strict=false，为什么导入失败？',
                   '补充：当前还没有提供完整错误日志。','纠正一下，刚才说错了','enable_insert_strict=true',
                   '纠正一下，我用的是 Doris 2.1.4','那比较3.x和4.x这个参数的规则',
                   '那具体含义呢？','换个问题，PI()返回什么？']
            states=[]
            for text in texts:
                assert not http.get('/api/status').json()['index_ready']
                r=http.post('/api/sessions/'+c['id']+'/turns',data={'query':text,'revision':c['revision']});assert r.status==202
                for _ in range(100):
                    c=http.get('/api/sessions/'+c['id']).json()
                    if not c['busy']:break
                    page.wait_for_timeout(30)
                assert not c['busy']
                api_results.append({'input':text,'state':copy.deepcopy(c['state']),'result':c['messages'][-1]['result']})
                states.append(copy.deepcopy(c['state']))
            checks['supplement_keeps_goal']=states[1]['environment_version']=='3.x' and 'enable_insert_strict' in states[1]['goal']
            checks['correction_clarification_answer_applied']='enable_insert_strict=false' not in states[3]['goal'] and 'enable_insert_strict=true' in states[3]['goal']
            checks['explicit_version_correction']=states[4]['environment_literal']=='2.1.4'
            checks['temporary_comparison_preserves_environment']=states[5]['environment_version']=='2.1' and states[6]['environment_version']=='2.1'
            checks['topic_switch_drops_facts']=states[7]['environment_version'] is None and 'enable_insert_strict' not in states[7]['goal']
            checks['zero_model_calls']=all(r['result'].get('timings',{}).get('llm_calls')==0 for r in api_results)
            checks['no_browser_errors']=not errors
        except Exception as error:
            errors.append(type(error).__name__+': '+str(error));checks['workflow_completed']=False
        finally:
            for sid in created:http.delete('/api/sessions/'+sid)
            browser.close();http.dispose()
    result={'passed':all(checks.values()),'checks':checks,'errors':errors,'real_http_state_steps':api_results,
            'limits':'网页与本机HTTP状态真实运行；答案及异常分类为指定回放/故障注入；没有模型调用或Dense问答验收。'}
    (OUT/(tag+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'checks':checks,'errors':errors},ensure_ascii=True))
    return result['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--tag',default='flow_review');args=parser.parse_args()
    OUT.mkdir(exist_ok=True);sys.exit(0 if run(args.tag) else 1)
