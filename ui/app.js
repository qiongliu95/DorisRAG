/* 用户材料用文本节点；Markdown HTML 只接受服务端禁用原始 HTML 的渲染结果。 */
const $ = id => document.getElementById(id);
let current = null, poll = null, epoch = 0, sending = false;
let recoveryTicket = null;
let creating = null, pendingSid = null;
const storageKey = 'dorisrag.v1.current';
const emptySessionKey = 'dorisrag.v1.empty';
const labels = {processing:'处理中', generated:'已完成', acknowledged:'已完成 · 已记录', needs_clarification:'已完成 · 需要补充', insufficient_evidence:'已完成 · 证据不足', no_evidence:'已完成 · 证据不足', execution_failed:'失败', interrupted:'失败 · 已中断', index_not_ready:'失败 · 知识库未就绪', understanding_failed:'失败 · 材料理解未完成', understanding_unavailable:'失败 · 生成服务未配置', generation_incomplete:'失败 · 回答未完成', answer_validation_failed:'失败 · 回答校验未通过', source_integrity_failed:'失败 · 原文校验未通过', llm_not_configured:'失败 · 生成服务未配置'};
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function error(message='') { $('error').textContent=message; $('error').hidden=!message; }
function remember(key, value) { try { if(value===null)localStorage.removeItem(key); else localStorage.setItem(key,value); } catch {} }
function remembered(key) { try{return localStorage.getItem(key);}catch{return null;} }
async function api(path, options={}) {
  let response;try{response=await fetch(path, {...options, headers:{'Content-Type':'application/json',...options.headers}});}catch{throw new Error('与问答服务的连接中断，请确认服务可用后再试。');}
  if(response.status===204)return null;
  let data;try{data=await response.json();}catch{const failure=new Error('服务响应无法解析，请稍后重试。');failure.status=response.status;throw failure;}
  if(!response.ok){const failure=new Error(typeof data.detail==='string'?data.detail:response.status===422?'提交信息不完整，请核对后再试。':'请求未完成，请稍后重试。');failure.status=response.status;throw failure;}
  return data;
}
function draftKey(id){return 'dorisrag.v1.draft.'+id;}
function setCurrent(doc) {
  current=doc;remember(storageKey,doc.id);$('session-title').textContent=doc.messages.length?doc.title:'Doris 知识助手';
  if(!doc.messages.length)remember(emptySessionKey,doc.id);
  else if(remembered(emptySessionKey)===doc.id)remember(emptySessionKey,null);
  render(); $('send').disabled=doc.busy||sending||pendingSid===doc.id;
}
async function history() {
  const list=(await api('/api/sessions')).filter(item=>!item.empty);$('history').replaceChildren();
  if(!list.length)$('history').append(node('p','还没有会话','muted'));
  for(const item of list){
    const row=node('div',undefined,'history-row'+(current?.id===item.id?' active':''));
    row.dataset.sessionId=item.id;
    const select=node('button',item.title,'history-item');select.title=item.title;select.onclick=()=>selectSession(item.id).catch(e=>error(e.message));
    if(current?.id===item.id)select.setAttribute('aria-current','true');
    const del=node('button','×','delete');del.setAttribute('aria-label','删除会话 '+item.title);
    del.onclick=async()=>{if(!confirm('删除这个会话及其历史记录？'))return;try{await api('/api/sessions/'+item.id,{method:'DELETE'});remember(draftKey(item.id),null);if(current?.id===item.id){clearTimeout(poll);current=null;remember(storageKey,null);await newSession();}else await history();}catch(e){error(e.message);}};
    const rename=node('button','改名','rename');rename.setAttribute('aria-label','重命名会话 '+item.title);
    rename.onclick=async()=>{try{const doc=await api('/api/sessions/'+item.id);const title=prompt('修改会话标题（最多40个字符）',doc.title);if(title===null)return;
      const updated=await api('/api/sessions/'+item.id,{method:'PATCH',body:JSON.stringify({title,revision:doc.revision})});if(current?.id===item.id)setCurrent(updated);await history();
    }catch(e){error(e.message);}};
    row.append(select,rename,del);$('history').append(row);
  }
}
async function selectSession(id){
  const ticket=++epoch;clearTimeout(poll);recoveryTicket=null;error();
  const doc=await api('/api/sessions/'+id);if(ticket!==epoch)return;
  if(pendingSid===id)pendingSid=null;
  setCurrent(doc);$('query').value=remembered(draftKey(id))||'';$('sidebar').classList.remove('open');
  await history();if(doc.busy)pollSession(id,ticket);
}
async function newSession(){
  if(creating)return creating;
  if(current&&!current.messages.length&&!current.busy){$('query').focus();return current;}
  creating=(async()=>{const ticket=++epoch;clearTimeout(poll);
    // 切回历史后再点新建，也复用本浏览器尚未发送过消息的空会话。
    const empty=remembered(emptySessionKey);
    if(empty){try{const saved=await api('/api/sessions/'+empty);if(ticket!==epoch)return;
      if(!saved.messages.length&&!saved.busy){await selectSession(empty);$('query').focus();return saved;}
      remember(emptySessionKey,null);
    }catch(e){if(e.status!==404)throw e;remember(emptySessionKey,null);}}
    const doc=await api('/api/sessions',{method:'POST'});if(ticket!==epoch)return;
    await selectSession(doc.id);if(current?.id===doc.id){$('query').focus();return doc;}
  })();try{return await creating;}finally{creating=null;}
}
function clearSentDraft(id,text){
  if(remembered(draftKey(id))?.trim()===text)remember(draftKey(id),null);
  if(current?.id===id&&$('query').value.trim()===text)$('query').value='';
}
async function replaceDeletedSession(id,draft){
  if(current?.id===id)current=null;
  const created=await newSession();if(!created||current?.id!==created.id)return;
  if(draft){$('query').value=draft;remember(draftKey(created.id),draft);}
  remember(draftKey(id),null);error('原会话已被删除，已新建会话。未发送内容已保留。');
}
function welcome(){
  const panel=node('section',undefined,'welcome');panel.append(node('div','Doris 文档知识助手','eyebrow'),node('h2','把问题说清，让答案有据可查。'),node('p','了解功能用法，分析报错日志，核对配置与版本条件。\n直接提问，或从一个例子开始。'));
  const guides=node('div',undefined,'guides');
  const examples=[['了解功能与用法','查询概念、SQL、参数和操作步骤','PI() 返回什么？请给出 SQL 示例。'],['分析报错与日志','结合原始材料，寻找有依据的解释','Doris 4.1.4 升级后，Upload Action 接口返回 HTTP 404，应该如何排查？'],['解决配置／功能异常','核对适用条件、限制与配置规则','Doris 3.0.2 中 hive.recursive_directories 有什么作用和使用条件？']];
  examples.forEach(([title,detail,text],i)=>{const b=node('button',undefined,'guide');b.type='button';b.append(node('small','0'+(i+1)),node('strong',title),node('span',detail),node('em','↗'));b.onclick=()=>{$('query').value=text;remember(draftKey(current.id),text);$('query').focus();};guides.append(b);});
  panel.append(guides,node('div','点击只会填入示例，你可以修改后再发送。','home-note'));return panel;
}
function safeLink(label,url){
  const link=node('a',label);try{const parsed=new URL(url,location.origin);if(!['http:','https:'].includes(parsed.protocol))return link;link.href=parsed.href;link.target='_blank';link.rel='noopener noreferrer';}catch{}return link;
}
function markdownView(html,text,cls='markdown'){
  const box=node('div',undefined,cls);
  if(typeof html==='string')box.innerHTML=html;else box.textContent=text||'';
  for(const block of [...box.querySelectorAll('pre,table')]){
    if(block.tagName==='PRE'?(block.textContent.split('\n').length>12||block.textContent.length>900):block.rows.length>9){
      const more=node('details',undefined,'long-content');more.append(node('summary',block.tagName==='PRE'?'展开完整代码':'展开完整表格'));
      block.replaceWith(more);more.append(block);
    }
  }return box;
}
function sourceLink(row,start=row.source_start_line,end=row.source_end_line){
  return '/api/source?doc_id='+encodeURIComponent(row.doc_id)+'&start_line='+start+'&end_line='+end+'#L'+start;
}
function evidenceView(result){
  const candidateOnly=result.evidence?.every(e=>e.evidence_role==='retrieval_candidate');
  const outer=node('section',undefined,'sources');outer.append(node('p',candidateOnly?'检索到的文档':'文档依据','sources-label'));
  const groups=result.evidence_groups||[];
  for(const group of groups){
    const first=group.sources[0], versions=[...new Set(group.sources.map(s=>s.source_version))].join(' / ');
    const d=node('details',undefined,'source');
    d.dataset.evidenceRanks=group.ranks.join(',');d.tabIndex=-1;
    const summary=node('summary',group.ranks.map(n=>'[证据 '+n+']').join(' ')+' '+first.title+' · Doris '+versions);
    summary.append(node('span',first.section_path||'文档正文','source-section'),node('span',group.excerpt?'原文节选：'+group.excerpt:'本段含代码或表格，展开核对原文。','source-preview'));d.append(summary);
    for(const row of group.sources){
      const info=node('div',undefined,'source-info');
      info.append(node('p','[证据 '+row.rank+'] Doris '+row.source_version+' · '+row.section_path+' · 原文第 '+row.source_start_line+'–'+row.source_end_line+' 行','meta'));
      info.append(safeLink('官方文档 ↗',row.source_url),safeLink('定位原文 ↗',sourceLink(row)));
      const conditions=[...(row.document_version_notices||[]),...(row.version_note||[])];
      if(conditions.length)info.append(node('p',[...new Set(conditions)].join('\n'),'condition'));
      d.append(info);
    }
    d.append(markdownView(group.html,'','evidence-text markdown'));
    if(group.contexts.length){
      const more=node('details',undefined,'supplemental');more.append(node('summary','补充上下文'));
      for(const c of group.contexts){
        for(const s of c.sources)more.append(safeLink('Doris '+s.source_version+' · 原文第 '+s.start+'–'+s.end+' 行 ↗',sourceLink(s,s.start,s.end)));
        more.append(markdownView(c.html,''));
      }
      d.append(more);
    }
    if(group.partial)d.append(node('p','补取内容不完整，请结合原文核对。','meta'));
    outer.append(d);
  }
  return outer;
}
function answerView(text,result){
  return markdownView(result?.answer_html,text,'answer markdown');
}
function citationLinks(article,result){
  const box=article.querySelector('.answer');if(!box)return;
  const walker=document.createTreeWalker(box,NodeFilter.SHOW_TEXT);const nodes=[];while(walker.nextNode())if(!walker.currentNode.parentElement.closest('pre,code,a,button'))nodes.push(walker.currentNode);
  for(const text of nodes){const re=/\[证据\s*(\d+)\]/g;let hit,last=0,fragment=document.createDocumentFragment(),matched=false;
    while((hit=re.exec(text.textContent))){matched=true;fragment.append(document.createTextNode(text.textContent.slice(last,hit.index)));
      const source=[...article.querySelectorAll('.source')].find(d=>d.dataset.evidenceRanks.split(',').includes(hit[1]));
      if(source){const b=node('button',hit[0],'citation');b.type='button';b.setAttribute('aria-label','查看证据 '+hit[1]);b.onclick=()=>{source.open=true;source.focus({preventScroll:true});source.scrollIntoView({behavior:'smooth',block:'center'});};fragment.append(b);}else fragment.append(document.createTextNode(hit[0]));last=re.lastIndex;
    }if(matched){fragment.append(document.createTextNode(text.textContent.slice(last)));text.replaceWith(fragment);}
  }
}
function render(){
  if(!current)return;
  const chat=$('chat'), nearBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<100;
  // 轮询只在修订变化时重绘，避免反复打断用户展开依据或阅读历史。
  const fragment=document.createDocumentFragment();if(!current.messages.length)fragment.append(welcome());
  for(const m of current.messages){const article=node('article',undefined,'message '+m.role);
    if(m.role==='user')article.append(node('div',m.content,'user-text'));
    else{
      const label=node('div',undefined,'assistant-label');label.append(node('span','D','mark'),node('span','DorisRAG'));
      const warn=['needs_clarification','insufficient_evidence','no_evidence'].includes(m.status),bad=!['generated','acknowledged','processing'].includes(m.status)&&!warn;
      article.dataset.state=m.status==='processing'?'processing':bad?'failed':'completed';
      label.append(node('span',labels[m.status]||'未完成','status-tag'+(bad?(warn?' warn':' error'):'')));article.append(label);
      if(m.status==='processing')article.append(node('p','正在处理，请稍候。完成后会自动显示；可以切换会话，刷新后也能继续查看。','thinking'));
      else article.append(answerView(m.content,m.result));
      const r=m.result;if(r){const t=r.timings||{};const scope=r.source_version||'';const parts=[];if(scope)parts.push('本次来源范围：'+scope);if(t.complete_seconds!==undefined)parts.push('完整耗时 '+t.complete_seconds.toFixed(2)+' 秒');if(parts.length)article.append(node('p',parts.join(' · '),'meta'));if(r.evidence?.length)article.append(evidenceView(r));
      }
      if(m.retry_available){const retry=node('button','重试','retry');retry.type='button';retry.disabled=current.busy||sending||pendingSid===current.id;retry.onclick=()=>retryMessage(m.id);article.append(retry);}
      else if(bad&&m===current.messages.at(-1)&&!current.busy)article.append(node('p','这条历史请求没有可安全恢复的重试快照，原问题和历史已保留。','meta'));
      citationLinks(article,r);
    }fragment.append(article);
  }chat.replaceChildren(fragment);if(nearBottom||current.messages.length<=2)chat.scrollTop=chat.scrollHeight;
}
function pollSession(id,ticket,submission=null){
  clearTimeout(poll);poll=setTimeout(async()=>{try{
    const doc=await api('/api/sessions/'+id);if(ticket!==epoch||current?.id!==id)return;
    if(pendingSid===id)pendingSid=null;
    // 确认响应丢失时只读回查，不重发；仅核对到本轮原文与紧邻修订后才清草稿。
    if(submission&&doc.revision>submission.revision){
      const user=doc.messages.at(-2);
      if(doc.revision<=submission.revision+2&&user?.role==='user'&&user.content.trim()===submission.text){clearSentDraft(id,submission.text);error();}
      submission=null;
    }
    // 只清除本轮轮询产生的断线提示，不覆盖其他会话或提交错误。
    if(recoveryTicket===ticket){error();recoveryTicket=null;}
    if(doc.revision!==current.revision||doc.busy!==current.busy)setCurrent(doc);
    if(doc.busy)pollSession(id,ticket,submission);else {await history();$('send').disabled=sending;for(const b of document.querySelectorAll('.retry'))b.disabled=sending;}
  }catch(e){if(ticket!==epoch)return;
    if(e.status===404){try{await replaceDeletedSession(id,$('query').value);}catch{error('会话已被删除，请新建会话；未发送内容已保留。');}return;}
    recoveryTicket=ticket;error(e.message+' 正在尝试恢复连接。');pollSession(id,ticket,submission);}},900);
}
$('question-form').onsubmit=async e=>{
  e.preventDefault();const text=$('query').value.trim();if(!text||!current||current.busy||sending||pendingSid===current.id)return;
  const sid=current.id,ticket=epoch,revision=current.revision;const started=performance.now();sending=true;$('send').disabled=true;$('send').textContent='提交中…';error();
  try{
    const data=await api('/api/sessions/'+sid+'/turns',{method:'POST',body:JSON.stringify({query:text,revision})});
    // 记录浏览器真实网络反馈耗时，不以服务端处理时间冒充用户等待时间。
    console.info('DorisRAG first accepted feedback ms',performance.now()-started);
    // 确认发出内容后只清除对应草稿；等待期间新输入的下一轮材料必须保留。
    clearSentDraft(sid,text);
    if(ticket===epoch){if($('query').value.trim()===text)$('query').value='';setCurrent(data.session);$('chat').scrollTop=$('chat').scrollHeight;pollSession(sid,ticket);}await history();
  }catch(e){if(ticket===epoch){error(e.message+' 输入已保留。');const uncertain=!e.status||e.status<400;if(uncertain){pendingSid=sid;recoveryTicket=ticket;}if(uncertain||e.status===409||e.status>=500)pollSession(sid,ticket,uncertain?{text,revision}:null);}}
  finally{sending=false;$('send').textContent='发送 ↗';$('send').disabled=!!current?.busy||pendingSid===current?.id;}
};
async function retryMessage(mid){
  if(!current||current.busy||sending||pendingSid===current.id)return;
  const sid=current.id,ticket=epoch,revision=current.revision,original=current.messages.at(-2)?.content;sending=true;$('send').disabled=true;for(const b of document.querySelectorAll('.retry'))b.disabled=true;error();
  try{const data=await api('/api/sessions/'+sid+'/messages/'+mid+'/retry',{method:'POST',body:JSON.stringify({revision})});
    if(original)clearSentDraft(sid,original);if(ticket===epoch){setCurrent(data.session);pollSession(sid,ticket);}await history();
  }catch(e){if(ticket===epoch){error(e.message);if(!e.status){pendingSid=sid;recoveryTicket=ticket;}pollSession(sid,ticket);}}
  finally{sending=false;$('send').disabled=!!current?.busy||pendingSid===current?.id;for(const b of document.querySelectorAll('.retry'))b.disabled=!!current?.busy||pendingSid===current?.id;}
}
$('query').addEventListener('input',()=>{if(current)remember(draftKey(current.id),$('query').value);});
$('query').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&!e.isComposing){e.preventDefault();$('question-form').requestSubmit();}});
$('new-session').onclick=()=>newSession().catch(e=>error(e.message));$('menu').onclick=()=>$('sidebar').classList.toggle('open');
$('close-menu').onclick=()=>$('sidebar').classList.remove('open');
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('sidebar').classList.remove('open');});
async function status(){try{const s=await api('/api/status');$('service-status').textContent=s.index_ready?(s.warmup_status==='failed'?'知识库准备失败':s.retrieval_ready?'知识库已就绪':'知识库准备中'):'知识库准备中';$('service-status').title=s.llm_configured?'生成服务已配置，连接状态以实际问答为准。':'生成服务尚未配置。';$('service-dot').classList.toggle('ready',s.retrieval_ready);$('version-hint').textContent=s.notice+(s.llm_configured?' 生成服务连接状态以实际问答为准。':' 生成服务尚未配置。');}catch{$('service-status').textContent='问答服务连接中断';$('service-dot').classList.remove('ready');}}
async function init(){await status();const id=remembered(storageKey);if(id){try{await selectSession(id);return;}catch(e){if(e.status!==404)throw e;remember(storageKey,null);await replaceDeletedSession(id,remembered(draftKey(id))||'');return;}}await newSession();}
init().catch(e=>error('无法恢复会话：'+e.message));setInterval(status,15000);
