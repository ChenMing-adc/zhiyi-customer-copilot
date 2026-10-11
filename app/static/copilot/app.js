const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let auth=null, sessions=[], ctx=null, run=null, epoch=0, cases=[], selected=null, feedbackRun=null, analysisAbort=null;
const notify=text=>{ $('toast').textContent=text; $('toast').hidden=false; clearTimeout(notify.timer); notify.timer=setTimeout(()=>$('toast').hidden=true,6000); };
async function api(path,body,signal){
  const response=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+auth.token}:{})},body:body===undefined?undefined:JSON.stringify(body),signal});
  const data=await response.json(); if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)); return data;
}
function guard(fn){return async(...args)=>{try{await fn(...args);}catch(e){if(e.name!=='AbortError')notify(e.message);}};}
function version(){return {mode:ctx.mode,cutoff:ctx.cutoff,version:ctx.version};}
function dialog(title,html){$('dialogTitle').textContent=title;$('dialogBody').innerHTML=html;if(!$('dialog').open)$('dialog').showModal();}
$('closeDialog').onclick=()=>$('dialog').close();
$('login').onclick=guard(async()=>{
  epoch++;analysisAbort?.abort();ctx=null;run=null;feedbackRun=null;$('reply').value='';$('reply').disabled=true;$('send').disabled=true;$('analyze').disabled=true;
  auth=await api('/demo/login',{name:$('identity').value});
  $('login').textContent='切换身份'; const status=await api('/status');
  $('modelInfo').textContent=status.provider+' · '+status.model;
  if(status.startup_error)notify('导入未完成：'+JSON.stringify(status.startup_error));
  sessions=await api('/sessions');
  $('scene').innerHTML='<option value="">全部场景</option>'+[...new Set(sessions.map(s=>s.scene))].sort().map(s=>`<option>${esc(s)}</option>`).join('');
  renderSessions(); await loadCases();
  if(sessions.length)await choose(sessions.find(s=>s.id==='S00053')?.id||sessions[0].id);
});
function renderSessions(){
  const q=$('search').value.trim().toLowerCase(),scene=$('scene').value;
  const rows=sessions.filter(s=>(!scene||s.scene===scene)&&(!q||(s.id+s.nickname+s.orders.join(' ')).toLowerCase().includes(q)));
  $('sessionCount').textContent=rows.length+' 个';
  $('sessionList').innerHTML=rows.map(s=>`<button class="session ${ctx?.session_id===s.id?'selected':''}" data-session="${esc(s.id)}"><div class="session-title"><span><span class="avatar">${esc(s.nickname[0])}</span>${esc(s.nickname)}</span><small>${esc(s.id)}</small></div><p>${esc(ctx?.session_id===s.id&&ctx.mode==='replay'?ctx.messages.at(-1)?.text:s.latest)}</p><span class="mini">${esc(s.scene)}</span> <small>${s.count} 条消息</small></button>`).join('')||'<div class="empty">没有匹配的会话</div>';
}
$('search').oninput=renderSessions;$('scene').onchange=renderSessions;
$('sessionList').onclick=guard(async e=>{const b=e.target.closest('[data-session]');if(b)await choose(b.dataset.session);});
async function choose(sid){
  const ticket=++epoch; analysisAbort?.abort();run=null;feedbackRun=null;
  $('reply').value='';$('reply').disabled=true;$('send').disabled=true;$('analyze').disabled=true;
  $('copilotContent').innerHTML='<div class="empty">读取事实与证据…</div>';
  if(ctx?.session_id!==sid)$('cutoff').value=1;
  const mode=$('mode').value,cutoff=mode==='replay'?Number($('cutoff').value)||1:null;
  const next=await api(`/sessions/${encodeURIComponent(sid)}/context?mode=${mode}${cutoff?'&cutoff='+cutoff:''}`);
  if(ticket!==epoch)return;
  ctx=next;renderSessions();renderContext();
}
function renderContext(){
  $('customer').textContent=ctx.nickname;
  $('customerMeta').textContent=ctx.session_id+' · '+ctx.store+' · 昵称不作为真实身份依据';
  $('cutoff').max=ctx.total;$('cutoff').disabled=ctx.mode!=='replay';
  $('previous').disabled=ctx.mode!=='replay'||ctx.cutoff<=1;$('next').disabled=ctx.mode!=='replay'||ctx.cutoff>=ctx.total;
  $('position').textContent=ctx.mode==='review'?`${ctx.total} 条原始消息`:`${ctx.cutoff} / ${ctx.total}`;
  $('messages').innerHTML=ctx.messages.map(m=>`<article class="message ${m.role==='客服'?'staff':m.role==='系统'?'system':''}" id="msg-${esc(m.id)}"><div class="message-meta"><span>${esc(m.role)}</span><span>${esc(m.time)}</span>${m.source_type==='demo_operation'?'<span>演示消息</span>':''}</div><div class="bubble">${esc(m.text)}</div></article>`).join('');
  $('messages').scrollTop=$('messages').scrollHeight;
  $('reply').disabled=ctx.mode==='replay';$('send').disabled=ctx.mode==='replay';$('analyze').disabled=false;$('analyze').textContent='分析会话';
  $('sendHint').textContent=ctx.mode==='replay'?'逐轮回放为只读；切换全量复盘可发送演示消息':'消息只写入独立演示环境';
  renderCopilot();
}
$('mode').onchange=guard(async()=>{if(ctx){$('cutoff').value=1;await choose(ctx.session_id);}});
$('cutoff').onchange=guard(async()=>{if(ctx)await choose(ctx.session_id);});
$('previous').onclick=guard(async()=>{$('cutoff').value=Number($('cutoff').value)-1;await choose(ctx.session_id);});
$('next').onclick=guard(async()=>{$('cutoff').value=Number($('cutoff').value)+1;await choose(ctx.session_id);});
function refs(ids){return ids.map(id=>`<button class="evidence" data-evidence="${esc(id)}">查看原文 ↗</button>`).join(' · ');}
function renderCopilot(){
  const facts=ctx.facts.map(f=>`<details class="fact" open><summary>${esc(f.type)}</summary><small>${esc(f.note)}</small><dl>${Object.entries(f.fields).map(([k,v])=>`<dt>${esc(k)}</dt><dd><button class="evidence" data-evidence="${esc(v.evidence_id)}">${esc(v.value)}</button></dd>`).join('')}</dl></details>`).join('');
  const risks=run?.risks||ctx.risk_candidates,promises=run?.promises||ctx.promise_candidates;
  $('copilotContent').innerHTML=`<section class="card lead"><h3>当前应做什么 <span class="tag">${esc(run?.intent||'待分析')}</span></h3><p>${esc(run?.next_action||'查看关联记录，核实消费者当前诉求')}</p><small>${ctx.mode==='replay'?'历史状态无法还原时显示未知':'当前为全量复盘，状态来自导入快照'}</small></section>
  ${run?`<section class="card"><h3>诉求与情绪 <span class="tag">${esc(run.emotion)}</span></h3><p>${esc(run.trend)}</p>${refs(run.emotion_evidence)}${run.summary.map(c=>`<p>${esc(c.text)} ${refs(c.evidence_ids)}</p>`).join('')}${run.missing_info.length?'<small>待核实</small><ul>'+run.missing_info.map(t=>'<li>'+esc(t)+'</li>').join('')+'</ul>':''}${run.error?'<p class="warning">'+esc(run.error)+'</p>':''}</section>`:''}
  <section class="card"><h3>事实与证据 <small>${ctx.facts.length} 条关联记录</small></h3>${facts||'<p>尚无关联订单或工单，可按售前咨询处理。</p>'}<small>点击字段查看原始表、行号与来源</small></section>
  <section class="card"><h3>建议回复 <small>人工审核后发送</small></h3>${run?.draft?`<p>${esc(run.draft)}</p><div class="actions"><button class="primary" id="fill">填入输入框</button><button id="reject">拒用并记录</button></div>`:`<p class="${run?'warning':''}">${run?'当前没有可用草稿，请人工核实后回复。':'点击「分析会话」生成有依据的建议。'}</p>`}</section>
  <section class="card"><h3>风险与承诺 <small>内部候选 · 待人工确认</small></h3>${risks.map((r,i)=>`<div class="risk high"><b>${esc(r.type)}</b><p>${esc(r.note)}</p>${refs(r.evidence_ids)}<div class="actions"><button data-risk="${i}">确认并创建跟进</button></div></div>`).join('')}${promises.map((p,i)=>`<div class="risk"><small>曾承诺 · 期限待确认</small><p>${esc(p.text)}</p>${refs(p.evidence_ids)}<div class="actions"><button data-promise="${i}">记录承诺跟进</button></div></div>`).join('')}${!risks.length&&!promises.length?'<p>当前未发现规则候选，可手动创建跟进。</p>':''}<div class="actions"><button id="manualFollowup">手动创建跟进</button></div></section>
  <section class="card"><h3>运行记录</h3><small>${run?`${esc(run.provider)} · ${esc(run.model)}<br>${run.elapsed_ms} ms · ${run.calls} 次调用 · ${run.tokens?run.tokens.total_tokens+' Token（服务商统计）':'Token 未提供'}<br>结果：${esc(run.status)} · ${esc(run.id.slice(0,8))}`:'尚未调用模型'}<br>演示 SOP：${esc(ctx.policy_version)}</small></section>`;
  $('fill')?.addEventListener('click',guard(async()=>{
    if(ctx.mode==='replay')return notify('回放模式只读，请在全量复盘重新生成草稿');
    $('reply').value=run.draft;feedbackRun=run.id;
    await api(`/analyses/${run.id}/feedback`,{outcome:'采用',reason:'填入编辑区，尚未发送'});notify('已填入，核对后点击发送演示消息');
  }));
  $('reject')?.addEventListener('click',()=>{
    const rid=run.id;dialog('记录拒用原因','<label>原因<textarea id="reason" placeholder="例如：未解决核心诉求、事实不准确"></textarea></label><button class="primary" id="saveFeedback">保存</button>');
    $('saveFeedback').onclick=guard(async()=>{await api(`/analyses/${rid}/feedback`,{outcome:'拒用',reason:$('reason').value});$('dialog').close();notify('已记录拒用原因');});
  });
  $('manualFollowup').onclick=()=>openFollowup({type:'人工跟进',priority:'常规',text:'',evidence_ids:[ctx.messages.filter(m=>m.role==='买家').at(-1)?.id||ctx.messages[0].id]});
}
$('copilotContent').onclick=e=>{
  const b=e.target.closest('[data-evidence],[data-risk],[data-promise]');if(!b)return;
  if(b.dataset.evidence){const ev=ctx.evidence[b.dataset.evidence];if(!ev)return;
    dialog('证据来源',`<span class="tag">${esc(ev.source_type==='official_mock'?'官方 MOCK':'演示操作')}</span><p>${esc(ev.file||'人工演示消息')}</p><p>${esc(ev.sheet||'')} · 第 ${esc(ev.row||'—')} 行 · ${esc(ev.field)}</p><pre>${esc(ev.value)}</pre>`);
    const msg=document.getElementById('msg-'+ev.id);if(msg){msg.scrollIntoView({block:'center'});msg.classList.add('highlight');setTimeout(()=>msg.classList.remove('highlight'),2500);}return;
  }
  if(b.dataset.risk!==undefined){const r=(run?.risks||ctx.risk_candidates)[Number(b.dataset.risk)];openFollowup({...r,text:r.type+'：核实并跟进处理'});}
  if(b.dataset.promise!==undefined){const p=(run?.promises||ctx.promise_candidates)[Number(b.dataset.promise)];openFollowup({...p,type:'承诺跟进',priority:'常规'});}
};
function openFollowup(item){
  const captured={...version(),session_id:ctx.session_id};
  dialog('确认跟进事项',`<p class="warning">历史承诺需核实；未确定期限可以留空。演示时间：${esc(ctx.data_time)}</p><label>事项内容<textarea id="followContent">${esc(item.text)}</textarea></label><label>责任人<select id="followOwner"><option value="">待认领</option><option>${esc(auth.name)}</option></select></label><label>确认的跟进期限（演示时间）<input id="followDue" type="datetime-local"></label><button class="primary" id="saveFollow">确认创建</button>`);
  $('saveFollow').onclick=guard(async()=>{const result=await api('/followups',{...captured,type:item.type,content:$('followContent').value,priority:item.priority,evidence_ids:item.evidence_ids,owner:$('followOwner').value,due_at:$('followDue').value||null});$('dialog').close();notify(result.duplicate?'该事项已存在，未重复创建':'已创建跟进事项');await loadCases();});
}
$('analyze').onclick=guard(async()=>{
  if(!ctx)return;const ticket=epoch, sid=ctx.session_id,requestVersion=version();
  analysisAbort?.abort();analysisAbort=new AbortController();
  $('analyze').disabled=true;$('analyze').textContent='正在分析…';
  $('copilotContent').insertAdjacentHTML('afterbegin','<div id="progress" class="card"><p>正在核实诉求与情绪，随后生成回复…</p><div class="spinner"></div><small>事实卡可继续查看，模型不可用时保留人工处理。</small></div>');
  try{const result=await api(`/sessions/${sid}/analyses`,requestVersion,analysisAbort.signal);if(ticket!==epoch)return;run=result;renderCopilot();}
  finally{if(ticket===epoch){$('progress')?.remove();$('analyze').disabled=false;$('analyze').textContent='重新分析';}}
});
$('send').onclick=guard(async()=>{
  if(!ctx||!$('reply').value.trim())return notify('请先填写回复');
  const sid=ctx.session_id,ticket=epoch;$('send').disabled=true;
  try{await api(`/sessions/${sid}/demo-messages`,{...version(),text:$('reply').value,run_id:feedbackRun,request_id:crypto.randomUUID()});if(ticket===epoch)await choose(sid);notify('演示消息已发送');}
  finally{if(ticket===epoch)$('send').disabled=false;}
});
async function loadCases(){if(!auth)return;cases=await api('/cases');$('caseCount').textContent=cases.filter(c=>!['已关闭','已排除'].includes(c.status)).length;renderCases();}
function renderCases(){
  const pending=cases.filter(c=>c.status==='待认领').length,active=cases.filter(c=>['处理中','待外部反馈','待回访','重新打开'].includes(c.status)).length,overdue=cases.filter(c=>c.overdue).length;
  $('metrics').innerHTML=[['待认领',pending,'等待明确责任人'],['正在处理',active,'持续跟进至结果确认'],['已逾期',overdue,'以演示时钟为准']].map(([title,note,sub])=>`<div class="metric">${title}<small>${sub}</small><b>${note}</b></div>`).join('');
  const filter=$('caseFilter').value,owner=$('ownerFilter').value;
  $('caseList').innerHTML=cases.filter(c=>(!owner||c.owner===owner)&&(!filter||(filter==='已逾期'?c.overdue:filter==='处理中'?['处理中','待外部反馈','待回访','重新打开'].includes(c.status):c.status===filter))).map(c=>`<article class="case-row"><div><b>${esc(c.type)}</b><p>${esc(c.session_id)} · ${esc(c.content.slice(0,65))}</p></div><div><span class="tag">${esc(c.status)}</span>${c.overdue?'<span class="danger">逾期</span>':''}<p>${esc(c.priority)}优先级</p></div><div>${esc(c.owner||'未认领')}<p>责任人</p></div><div>${esc(c.due_at||'期限待确认')}<p>演示期限</p></div><button data-case="${esc(c.id)}">查看与处理 →</button></article>`).join('')||'<div class="card empty">暂无符合条件的事项。可在客服副驾中确认风险或承诺，创建第一条跟进。</div>';
}
$('caseFilter').onchange=renderCases;$('ownerFilter').onchange=renderCases;
$('caseList').onclick=e=>{const b=e.target.closest('[data-case]');if(b)openCase(cases.find(c=>c.id===b.dataset.case));};
function openCase(c){
  selected=c;dialog('跟进详情 · '+c.session_id,`<div class="tag">${esc(c.status)}</div><p>${esc(c.content)}</p><small>责任人：${esc(c.owner||'未认领')} · v${c.version}<br>期限：${esc(c.due_at||'待确认')}<br>演示时钟：${esc(c.clock)}</small><h3>来源证据</h3>${c.evidence.map(e=>`<pre>${esc(e.value)}\n${esc(e.sheet||'演示消息')} · 行 ${esc(e.row||'—')}</pre>`).join('')}<label>操作<select id="caseAction"><option value="claim">认领</option><option value="note">记录跟进</option><option value="wait">等待外部反馈</option><option value="callback">等待回访</option><option value="progress">继续处理</option><option value="deadline">调整期限（主管）</option><option value="assign">改派（主管）</option><option value="close">补证关闭（主管）</option><option value="exclude">排除（主管）</option><option value="reopen">重新打开（主管）</option><option value="advance">推进一天演示时间（主管）</option></select></label><label>处理结果 / 原因<textarea id="caseNote"></textarea></label><label>结果证据 / 重开证据（演示操作请注明）<textarea id="caseEvidence" placeholder="例如：团队自建模拟审核结果，或人工核实记录"></textarea></label><label>调整后的期限<input id="caseDue" type="datetime-local"></label><label>改派给<select id="caseOwner"><option>客服 A</option><option>客服 B</option><option>主管</option></select></label><button class="primary" id="saveCase">提交操作</button><h3 style="margin-top:20px">处理时间线</h3>${c.history.map(h=>`<div class="history">${esc(h.actor)} · ${esc(h.action)}<p>${esc(h.detail.note||h.detail.content||'')}</p><small>${esc(h.at)}</small></div>`).join('')}`);
  if(c.result||c.completion_evidence){
    $('dialogBody').insertAdjacentHTML('afterbegin',`<section class="card lead"><h3>最近一次关闭的结果与证据</h3><p>${esc(c.result)}</p><pre>${esc(c.completion_evidence)}</pre><small>保留人工操作记录，不覆盖官方工单快照。</small></section>`);
  }
  const actionLabels={claim:'认领',note:'记录跟进',wait:'等待外部反馈',callback:'等待回访',progress:'继续处理',deadline:'调整期限',assign:'改派',close:'补证关闭',exclude:'排除',reopen:'重新打开',advance:'推进演示时钟',create_followup:'创建跟进'};
  $('dialogBody').querySelectorAll('.history').forEach((element,index)=>{
    const h=c.history[index];
    element.innerHTML=`${esc(h.actor)} · ${esc(actionLabels[h.action]||h.action)}<p>${esc(h.detail.note||h.detail.content||'')}</p>${h.detail.evidence?'<p>证据：'+esc(h.detail.evidence)+'</p>':''}<small>${esc(h.at)}</small>`;
  });
  $('saveCase').onclick=guard(async()=>{await api(`/cases/${c.id}/actions`,{version:c.version,action:$('caseAction').value,note:$('caseNote').value,evidence:$('caseEvidence').value,due_at:$('caseDue').value||null,owner:$('caseOwner').value});await loadCases();openCase(cases.find(row=>row.id===c.id));notify('事项已更新');});
}
function tab(board){$('workspace').hidden=board;$('board').hidden=!board;$('boardTab').classList.toggle('active',board);$('workspaceTab').classList.toggle('active',!board);}
$('workspaceTab').onclick=()=>tab(false);$('boardTab').onclick=guard(async()=>{tab(true);await loadCases();});$('refreshBoard').onclick=guard(loadCases);
