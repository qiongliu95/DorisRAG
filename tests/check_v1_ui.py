"""独立无头 Edge 页面验收，不读取或控制用户已有浏览器会话。"""
import json
import sys
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/v1_integration_20261024'
BASE='http://127.0.0.1:8765'
sys.stdout.reconfigure(encoding='utf-8')


def main():
    checks={};errors=[];created=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
        context=browser.new_context(viewport={'width':1366,'height':900})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(BASE,wait_until='networkidle')
        page.wait_for_selector('.guide')
        created.append(page.evaluate('current.id'))
        (OUT/'ui_test_sessions.json').write_text(json.dumps(created),encoding='utf-8')
        checks['three_guides']=page.locator('.guide').count()==3
        page.screenshot(path=str(OUT/'ui_home_desktop.png'),full_page=True)
        page.locator('.guide').first.click()
        checks['editable_example_not_sent']=page.locator('#query').input_value().startswith('PI()') and page.locator('.message').count()==0
        page.locator('#query').fill('帮我看看')
        page.locator('#send').click()
        page.wait_for_function('current && !current.busy && current.messages.length===2',timeout=30000)
        checks['necessary_clarification']=page.locator('.status-tag').last.inner_text()=='需要澄清'
        sid=page.evaluate('current.id');messages=page.locator('.message').count()
        page.reload(wait_until='networkidle');page.wait_for_selector('.message')
        checks['refresh_restores_current']=page.evaluate('current.id')==sid and page.locator('.message').count()==messages
        page.locator('#new-session').click();page.wait_for_selector('.guide')
        second=page.evaluate('current.id');created.append(second)
        (OUT/'ui_test_sessions.json').write_text(json.dumps(created),encoding='utf-8')
        checks['new_session']=second!=sid and page.locator('.message').count()==0
        page.locator('[data-session-id="'+sid+'"] .history-item').click();page.wait_for_selector('.message')
        checks['switch_history']=page.evaluate('current.id')==sid
        page.locator('#query').fill('我想知道 PI() 返回什么？')
        page.locator('#send').click()
        page.wait_for_function('current && !current.busy && current.messages.length===4',timeout=360000)
        last_status=page.locator('.status-tag').last.inner_text()
        ready=context.request.get(BASE+'/api/status').json()['index_ready']
        checks['honest_index_state']=ready or last_status=='索引尚未就绪'
        if ready:
            checks['live_generated_answer']=last_status=='完成'
            checks['live_sources']=page.locator('.sources').count()>0
            checks['live_version_scope']=page.evaluate('current.messages.at(-1).result.source_version')=='4.x'
        checks['version_hint_visible']=page.locator('#version-hint').is_visible() and '默认仅查询' in page.locator('#version-hint').inner_text()
        page.screenshot(path=str(OUT/'ui_conversation_desktop.png'),full_page=True)
        component_path=OUT/'component_cases.jsonl'
        if not ready and component_path.exists():
            records=[json.loads(line) for line in component_path.read_text(encoding='utf-8').splitlines()]
            replay=next((r['result'] for r in records if r['result']['status']=='generated' and r['result'].get('evidence')),None)
            if replay:
                # 展示回放仅验证前端依据组件；不是该生产服务实际运行过 Dense 的证明。
                original=context.request.get(BASE+'/api/sessions/'+sid).json()
                original['messages'][-1].update(content=replay['answer'],status='generated',result=replay)
                page.route('**/api/sessions/'+sid,lambda route:route.fulfill(json=original))
                page.reload(wait_until='networkidle');page.wait_for_selector('.sources')
                page.locator('.sources').last.locator('summary').first.click()
                page.locator('.source').last.locator('summary').first.click()
                checks['evidence_display_replay']=page.locator('.source').last.locator('a').count()==2 and 'Doris ' in page.locator('.source').last.inner_text()
                page.screenshot(path=str(OUT/'ui_evidence_component_replay.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844});page.locator('#menu').click()
        page.wait_for_function("document.getElementById('sidebar').getBoundingClientRect().left>=-0.5")
        checks['mobile_history']=page.locator('#sidebar').evaluate("e=>e.getBoundingClientRect().left>=-0.5")
        page.screenshot(path=str(OUT/'ui_mobile_history.png'),full_page=True)
        page.locator('#close-menu').click()
        page.wait_for_function("!document.getElementById('sidebar').classList.contains('open')")
        checks['mobile_no_horizontal_overflow']=page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.screenshot(path=str(OUT/'ui_mobile.png'),full_page=True)
        # 只删除本脚本自己创建的测试会话，保留用户历史。
        for sid in created:
            response=context.request.delete(BASE+'/api/sessions/'+sid)
            checks['test_session_delete']=response.status==204
        checks['no_browser_script_errors']=not errors
        browser.close()
    report={'passed':all(checks.values()),'checks':checks,'script_errors':errors,
            'full_dense_ui_answer_verified':ready and all(checks.values()),
            'limits':'索引未完成时仅验证实际界面/会话/澄清/失败状态；组件答案回放只证明展示，不证明完整问答。' if not ready else '真实页面/HTTP/LLM/活动新索引链路执行检查，答案仍须对照证据复核。'}
    (OUT/'ui_checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False));return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output-subdir')
    args=parser.parse_args()
    if args.output_subdir:
        if Path(args.output_subdir).name!=args.output_subdir:raise ValueError('输出子目录名称不合法')
        OUT=OUT/args.output_subdir;OUT.mkdir(exist_ok=True)
    sys.exit(0 if main() else 1)
