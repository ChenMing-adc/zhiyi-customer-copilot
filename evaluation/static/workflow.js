let reviewSample=null,reviewKey='',reviewRevision=0;
const post=(url,data)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
$('review-close').onclick=()=>$('review-dialog').close();
$('baseline-close').onclick=()=>$('baseline-dialog').close();
$('review-open').onclick=async()=>{
 if(demo||!selected){notice('请先在真实数据中选中一条样本。');return;}
 try{
 reviewKey=$('dataset').value;
 const d=await api(`/api/sample/${encodeURIComponent(reviewKey)}?${new URLSearchParams({sample_id:selected,split:$('split').value})}`);
 reviewSample=d.sample;reviewRevision=d.review.revision;
 $('review-context').innerHTML=`<p>${esc(d.label_policy)} · ${esc(d.policy_version)}</p>`+d.sample.input.messages.map(m=>`<div class="review-message"><b>${esc(m.role)}</b> ${esc(m.text)}</div>`).join('');
 $('review-labels').innerHTML=Object.entries(state.dataset.labels).map(([k,values])=>k==='risk'?`<fieldset><legend>风险候选</legend>${values.map(v=>`<label class="checkbox"><input type="checkbox" name="gold-risk" value="${esc(v)}">${esc(label(v))}</label>`).join('')}<label>风险核对<select required><option value="">请明确核对风险</option><option value="confirmed">已核对（未勾选任何候选表示无风险）</option></select></label></fieldset>`:`<label>${k==='emotion'?'当前情绪':'主要意图'}<select id="gold-${k}" required><option value="">请选择</option>${values.map(v=>`<option value="${esc(v)}">${esc(label(v))}</option>`).join('')}</select></label>`).join('');
 $('review-evidence').innerHTML='<b>消息依据（至少一条）</b>'+d.sample.input.messages.map(m=>`<label class="checkbox"><input type="checkbox" name="evidence" value="${esc(m.id)}">${esc(m.text.slice(0,100))}</label>`).join('');
 $('review-note').value='';$('review-action').value='vote';
 $('review-status').textContent=`当前状态：${d.annotation.review_status} · 版本 ${reviewRevision}。已有 ${Object.keys(d.review.votes).length} 人提交。`;
 $('review-action').onchange=()=>{if($('review-action').value==='adjudicate')$('review-status').textContent=d.review.history.map(h=>`${h.reviewer} · ${JSON.stringify(h.gold)} · ${h.note}`).join('\n')||'尚无双人分歧';};
 $('review-dialog').showModal();
 }catch(e){notice(e.message);}
};
$('review-form').onsubmit=async e=>{
 e.preventDefault();const button=e.submitter;button.disabled=true;
 try{
 const gold={emotion:$('gold-emotion').value,intent:$('gold-intent').value};
 if(document.querySelector('[name="gold-risk"]'))gold.risk=Array.from(document.querySelectorAll('[name="gold-risk"]:checked')).map(x=>x.value);
 const d=await post(`/api/review/${encodeURIComponent(reviewKey)}`,{sample_id:reviewSample.sample_id,revision:reviewRevision,reviewer:$('reviewer').value,gold,note:$('review-note').value,evidence_ids:Array.from(document.querySelectorAll('[name="evidence"]:checked')).map(x=>x.value),action:$('review-action').value});
 reviewRevision=d.revision;
 $('review-status').textContent=`已保存：${d.resolved.review_status} · 版本 ${d.revision}。\n`+d.history.map(h=>`${h.reviewer} · ${h.action} · ${JSON.stringify(h.gold)} · ${h.note}`).join('\n');
 // Reload counts without losing the open review form.
 await load();
 }catch(err){$('review-status').textContent=err.message;}finally{button.disabled=false;}
};
let baselineKey='';
$('baseline-open').onclick=async()=>{
 if(demo){notice('请先返回真实数据。');return;}
 try{
 baselineKey=$('dataset').value;
 const d=await api(`/api/baseline/preview/${encodeURIComponent(baselineKey)}`);
 $('baseline-summary').textContent=`${d.host} · ${d.model} · ${d.count} 条 · 双人已复核 ${d.reviewed} 条；保持现有 .env 参数。`;
 $('prompt-text').value=d.prompt;$('prompt-version').value=d.prompt_version;
 $('outbound-preview').textContent=JSON.stringify(d.samples,null,2);
 $('scope-confirm').checked=false;$('baseline-status').textContent='';$('baseline-dialog').showModal();
 }catch(e){notice(e.message);}
};
$('baseline-start').onclick=async()=>{
 if(!$('scope-confirm').checked){$('baseline-status').textContent='请先核对并确认外发范围。';return;}
 $('baseline-start').disabled=true;
 try{
 const d=await post(`/api/baseline/${encodeURIComponent(baselineKey)}`,{confirmed_scope:'official:S00053',prompt:$('prompt-text').value,prompt_version:$('prompt-version').value});
 $('split').value='dev';$('search').value='';$('errors').checked=false;
 $('run').innerHTML=`<option value="${d.id}">运行中</option>`;$('run').value=d.id;
 $('baseline-dialog').close();await load();
 }catch(e){$('baseline-status').textContent=e.message;}finally{$('baseline-start').disabled=false;}
};
$('run-stop').onclick=async()=>{try{const d=await post(`/api/stop/${$('run').value}`,{});notice(d.note);}catch(e){notice(e.message);}};
$('rescore').onclick=async()=>{
 if(demo||!$('run').value){notice('请先选择一条真实评测记录。');return;}
 try{const d=await post(`/api/rescore/${encodeURIComponent($('dataset').value)}`,{run_id:$('run').value,split:$('split').value});$('run').innerHTML=`<option value="${d.id}">复核后重算</option>`;await load();notice('已生成新的计分记录，无额外模型调用，原记录保留。');}catch(e){notice(e.message);}
};
let polling=false;
setInterval(async()=>{
 const run=state?.run;
 $('run-stop').disabled=demo||run?.status!=='running';
 $('rescore').disabled=demo||!run||run.status==='running';
 $('run-progress').textContent=demo?'人工示例，不调用模型':run?.status?`运行状态：${({running:'运行中',completed:'已完成',failed:'失败，已停止后续调用',cancelled:'已停止',interrupted:'服务重启中断'})[run.status]||run.status} · ${state.metrics.received_predictions}/${state.counts.samples} · ${run.model||''} · ${run.prompt_version}`:'未选择在线评测任务';
 if(!demo&&run?.status==='running'&&!polling){polling=true;try{const sid=selected;await load();if(sid)await showSample(sid);}finally{polling=false;}}
},2500);
