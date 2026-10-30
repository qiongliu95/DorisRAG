"""现有Playwright体系的隔离V1页面验收；本地假问答，不使用用户服务/历史或真实模型。"""
import copy
import json
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import requests
import uvicorn
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/v1_ux_20261028'
sys.path.insert(0,str(ROOT))
from doris_rag import web,telemetry
from doris_rag.common import sha256_file
from doris_rag.sessions import SessionStore


def main():
    checks={};calls=[];errors=[];mode={'value':'hold'};release=threading.Event()
    cfg=web.config();build=json.loads((Path(cfg['data_dir'])/'index/current.json').read_text(encoding='utf-8'))['build_id']
    future=Future();future.set_result({'build_id':build,'seconds':0.0})
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        root=Path(tmp);source=root/'source.md'
        original='原文说明：该示例用于验证证据展示。\n\n```sql\n'+'\n'.join('SELECT '+str(n)+';' for n in range(18))+'\n```\n\n| 字段 | 值 |\n| -- | -- |\n'+''.join(f'| row{n} | {n} |\n' for n in range(18))
        source.write_text(original,encoding='utf-8');end=len(original.splitlines())
        rows=[{'rank':n,'chunk_id':'fixture-private-chunk-'+str(n),'doc_id':'fixture-private-doc-'+str(n),
            'source_version':v,'title':'证据展示示例','section_path':'示例 > 用法','source_start_line':1,'source_end_line':end,
            'source_url':'https://doris.apache.org/zh-CN/docs/'+v+'/fixture', 'chunk_text':original,
            'source_sha256':sha256_file(source),'original_path':str(source),'context_status':'complete',
            'contexts':[{'text':'补充上下文完整原文。','source_start_line':1,'source_end_line':1}],
            'evidence_role':'answer_support'} for n,v in [(1,'4.x'),(2,'3.x')]]
        db=SessionStore(root/'sessions.db')
        def fake_query(query,version=None,**kwargs):
            calls.append(copy.deepcopy({'query':query,'version':version,**{k:kwargs.get(k) for k in ('version_scope','fact_sources','analysis_seed')}}))
            if mode['value']=='hold':release.wait(15)
            if mode['value']=='ReadTimeout':raise requests.ReadTimeout('fixture secret')
            if mode['value']=='ConnectionError':raise requests.ConnectionError('fixture secret')
            if mode['value']=='503':
                response=requests.Response();response.status_code=503
                raise requests.HTTPError('fixture secret',response=response)
            return {'status':'generated','answer':'## 示例回答\n\n这是一个用于界面验证的完成结果。[证据 1]\n\n- 说明一\n- 说明二\n\n另一个版本也可核对。[证据 2]\n\n'+original,
                'evidence':copy.deepcopy(rows),'source_version':'4.x,3.x','citation_numbers':[1,2],
                'understanding':{'status':'ready','versions':['3.x'],'goal':'PI()用法','subqueries':[]},'timings':{'llm_calls':0}}
        request_trace=web.request_trace
        def fixture_trace(detailed=False,identity=None,**kwargs):
            return request_trace(detailed,{**(identity or {}),'test_fixture':True,'real_model_calls':0},**kwargs)
        for target,value in [('store',db),('index_ready',lambda:True),('warm_future',future),('run_query',fake_query),
                             ('llm_config',lambda:{'model':'fixture','base_url':'fixture','api_key':'fixture'}),('request_trace',fixture_trace),
                             ('config',lambda:{**cfg,'source_root':str(root)}),('read_jsonl',lambda _:rows)]:
            stack.enter_context(patch.object(web,target,value))
        stack.enter_context(patch.object(telemetry,'TRACE_DIR',OUT/'browser_fixture_traces'))
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('no provider network')))
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(web.app,log_level='error'))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
                context=browser.new_context(viewport={'width':1366,'height':900})
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(f'http://127.0.0.1:{port}/') else route.abort())
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{port}',wait_until='networkidle');page.wait_for_selector('.guide')
                sid=page.evaluate('current.id')
                for _ in range(4):page.locator('#new-session').click()
                checks['empty_new_reused']=len(db.list())==1 and page.evaluate('current.id')==sid
                checks['home_layout_retained']=page.locator('.guide').count()==3
                checks['knowledge_status_not_model_health']='实际问答' in page.locator('#version-hint').inner_text()
                page.screenshot(path=str(OUT/'home.png'),full_page=True)
                page.locator('#query').fill('Doris 3.x 的 PI() 怎么用？');page.locator('#send').click()
                page.wait_for_selector('[data-state=processing]')
                checks['processing_real_feedback']=page.locator('#send').is_disabled() and '%' not in page.locator('.thinking').inner_text()
                page.locator('#new-session').click();page.wait_for_selector('.guide');empty_sid=page.evaluate('current.id')
                page.locator('[data-session-id="'+sid+'"] .history-item').click();page.wait_for_selector('[data-state=processing]')
                page.locator('#new-session').click();page.wait_for_selector('.guide')
                checks['empty_reused_after_history_switch']=page.evaluate('current.id')==empty_sid and len(db.list())==2
                page.locator('[data-session-id="'+sid+'"] .history-item').click();page.wait_for_selector('[data-state=processing]')
                page.reload(wait_until='networkidle');page.wait_for_selector('[data-state=processing]')
                checks['refresh_switch_processing_no_resubmit']=len(calls)==1 and len(db.get(sid)['messages'])==2
                release.set();mode['value']='generated'
                page.wait_for_selector('[data-state=completed]')
                checks['completed_status']=page.locator('.status-tag').last.inner_text()=='已完成'
                checks['markdown_headers_lists_tables']=page.locator('.answer h2').count()>0 and page.locator('.answer li').count()==2 and page.locator('.answer table').count()==1
                checks['long_blocks_folded']=page.locator('.answer .long-content:not([open])').count()==2
                checks['compact_sources']=page.locator('.source:not([open])').count()==1 and '原文节选' in page.locator('.source > summary').inner_text()
                page.locator('.citation').first.click()
                checks['citation_opens_source']=page.locator('.source[open]').count()==1
                checks['cross_version_source_mapping']='4.x / 3.x' in page.locator('.source > summary').inner_text()
                checks['evidence_long_sql_table_folded']=page.locator('.source .long-content:not([open])').count()==2
                page.locator('.source .long-content').first.locator('summary').click()
                checks['full_sql_retained']='SELECT 17;' in page.locator('.source pre').inner_text()
                with page.expect_popup() as popup:
                    page.locator('.source a',has_text='定位原文').first.click()
                raw=popup.value;raw.wait_for_load_state();checks['source_range_verifiable']=raw.locator('.hit').count()==end;raw.close()
                page.screenshot(path=str(OUT/'answer_evidence.png'),full_page=True)
                mode['value']='ReadTimeout';page.locator('#query').fill('那所有版本都查一下');page.locator('#send').click()
                page.wait_for_selector('.retry');page.wait_for_function('!current.busy')
                failed=db.get(sid);mid=failed['messages'][-1]['id'];frozen=calls[-1]
                checks['timeout_chinese']='响应超时' in page.locator('.message.assistant').last.inner_text() and 'ReadTimeout' not in page.locator('body').inner_text()
                page.reload(wait_until='networkidle');page.wait_for_selector('.retry')
                checks['failed_refresh_restored']=len(db.get(sid)['messages'])==4
                mode['value']='ConnectionError';before=len(calls);page.locator('.retry').dblclick()
                page.wait_for_function('!current.busy && current.messages.at(-1).result.error_type==="ConnectionError"')
                checks['connection_chinese']='无法连接' in page.locator('.message.assistant').last.inner_text()
                checks['double_retry_no_duplicate']=len(calls)==before+1 and len(db.get(sid)['messages'])==4
                mode['value']='503';page.locator('.retry').click();page.wait_for_function('!current.busy && current.messages.at(-1).result.http_status===503')
                checks['unavailable_chinese']='暂时不可用' in page.locator('.message.assistant').last.inner_text()
                page.screenshot(path=str(OUT/'failure_retry.png'),full_page=True)
                mode['value']='generated';page.locator('.retry').click();page.wait_for_function('!current.busy && current.messages.at(-1).status==="generated"')
                checks['retry_same_turn_scope_and_material']=all(c==frozen for c in calls[1:]) and db.get(sid)['messages'][-1]['id']==mid and len(db.get(sid)['messages'])==4
                page.once('dialog',lambda d:d.accept('多版本问答记录'))
                page.locator('[data-session-id="'+sid+'"] .rename').click();page.wait_for_function('current.title==="多版本问答记录"')
                page.reload(wait_until='networkidle');page.wait_for_selector('.message')
                checks['rename_refresh_persisted']=page.locator('#session-title').inner_text()=='多版本问答记录'
                # 服务端已受理而浏览器没收到确认时只能回查，不得再次POST。
                def lost_ack(route):
                    route.fetch();route.abort()
                page.route('**/turns',lost_ack);mode['value']='ReadTimeout';before=len(calls)
                page.locator('#query').fill('那具体怎么用？');page.locator('#send').click()
                page.wait_for_function('current.messages.length===6 && !current.busy && document.getElementById("query").value===""')
                page.unroute('**/turns',lost_ack)
                checks['accepted_response_lost_readback_no_duplicate']=len(calls)==before+1 and len(db.get(sid)['messages'])==6
                retry_pattern='**/messages/*/retry'
                def disconnected(route):route.abort()
                page.route(retry_pattern,disconnected);before=len(calls);page.locator('.retry').click()
                page.wait_for_function('pendingSid===null && !document.getElementById("error").hidden===false && !document.querySelector(".retry").disabled')
                checks['unaccepted_retry_connection_recovery']=len(calls)==before and len(db.get(sid)['messages'])==6
                page.unroute(retry_pattern,disconnected)
                page.route(retry_pattern,lost_ack);mode['value']='generated';before=len(calls);page.locator('.retry').click()
                page.wait_for_function('!current.busy && current.messages.at(-1).status==="generated" && document.getElementById("error").hidden')
                page.unroute(retry_pattern,lost_ack)
                checks['retry_response_lost_no_duplicate']=len(calls)==before+1 and len(db.get(sid)['messages'])==6
                visible=page.locator('body').inner_text()
                checks['no_internal_ids_in_normal_ui']=all(t not in visible for t in [sid,mid,'Trace','Request ID','Message ID','Chunk ID','fixture-private-chunk','fixture-private-doc','fixture secret'])
                checks['no_legacy_english']='DORIS KNOWLEDGE ASSISTANT' not in visible and 'Search before asking' not in visible
                page.set_viewport_size({'width':390,'height':844})
                page.wait_for_function('document.getElementById("sidebar").getBoundingClientRect().right<=1')
                checks['mobile_no_page_overflow']=page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
                checks['no_browser_errors']=not errors
                browser.close()
        finally:
            release.set();server.should_exit=True;thread.join(10);sock.close()
        report={'passed':all(checks.values()),'checks':checks,'errors':errors,'calls':calls,
                'real_model_calls':0,'fixture_only':True,'production_sessions_modified':False,'streaming':'not_supported; no partial success claimed'}
        (OUT/'browser_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'passed':report['passed'],'checks':checks,'errors':errors},ensure_ascii=False))
        return report['passed']

if __name__=='__main__':
    sys.exit(0 if main() else 1)
