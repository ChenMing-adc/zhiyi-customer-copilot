let reviewSample=null,reviewKey='',reviewRevision=0;
const post=(url,data)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
$('review-close').onclick=()=>$('review-dialog').close();
$('baseline-close').onclick=()=>$('baseline-dialog').close();
async function openReview(sampleId=selected){
 if(demo||!sampleId){notice('请先在真实数据中选中一条样本。');return;}
 try{
 selected=sampleId;await showSample(sampleId);
 reviewKey=$('dataset').value;
 const d=await api(`/api/sample/${encodeURIComponent(reviewKey)}?${new URLSearchParams({sample_id:sampleId,split:$('split').value})}`);
 reviewSample=d.sample;reviewRevision=d.review.revision;
 $('review-context').innerHTML=`<p>${esc(d.label_policy)} · ${esc(d.policy_version)}</p>`+d.sample.input.messages.map(m=>`<div class="review-message"><b>${esc(m.role)}</b> ${esc(m.text)}</div>`).join('');
 $('review-labels').innerHTML=Object.entries(state.dataset.labels).map(([k,values])=>k==='risk'?`<fieldset><legend>风险候选</legend>${values.map(v=>`<label class="checkbox"><input type="checkbox" name="gold-risk" value="${esc(v)}">${esc(label(v))}</label>`).join('')}<label>风险核对<select required><option value="">请明确核对风险</option><option value="confirmed">已核对（未勾选任何候选表示无风险）</option></select></label></fieldset>`:`<label>${k==='emotion'?'当前情绪':'主要意图'}<select id="gold-${k}" required><option value="">请选择</option>${values.map(v=>`<option value="${esc(v)}">${esc(label(v))}</option>`).join('')}</select></label>`).join('');
 $('review-evidence').innerHTML='<b>消息依据（至少一条）</b>'+d.sample.input.messages.map(m=>`<label class="checkbox"><input type="checkbox" name="evidence" value="${esc(m.id)}">${esc(m.text.slice(0,100))}</label>`).join('');
 $('review-note').value=d.annotation.note||'';
 for(const k of ['emotion','intent'])if(d.annotation.gold[k])$('gold-'+k).value=d.annotation.gold[k];
 for(const el of document.querySelectorAll('[name=gold-risk]'))el.checked=(d.annotation.gold.risk||[]).includes(el.value);
 for(const el of document.querySelectorAll('[name=evidence]'))el.checked=(d.annotation.evidence_ids||[]).includes(el.value);
 $('review-status').textContent=`当前状态：${d.annotation.review_status} · 版本 ${reviewRevision}。保存一次即可计分。`;
 $('review-dialog').showModal();
 }catch(e){notice(e.message);}
};
$('review-form').onsubmit=async e=>{
 e.preventDefault();const button=e.submitter;button.disabled=true;
 try{
 const gold={emotion:$('gold-emotion').value,intent:$('gold-intent').value};
 if(document.querySelector('[name="gold-risk"]'))gold.risk=Array.from(document.querySelectorAll('[name="gold-risk"]:checked')).map(x=>x.value);
 const d=await post(`/api/review/${encodeURIComponent(reviewKey)}`,{sample_id:reviewSample.sample_id,revision:reviewRevision,reviewer:$('reviewer').value,gold,note:$('review-note').value,evidence_ids:Array.from(document.querySelectorAll('[name="evidence"]:checked')).map(x=>x.value)});
 reviewRevision=d.revision;
 $('review-status').textContent=`已保存：${d.resolved.review_status} · 版本 ${d.revision}。\n`+d.history.map(h=>`${h.reviewer} · ${h.action} · ${JSON.stringify(h.gold)} · ${h.note}`).join('\n');
 selected=reviewSample.sample_id;
 await load();
 }catch(err){$('review-status').textContent=err.message;}finally{button.disabled=false;}
};
let baselineKey='',baselineSplit='',batchPreview=null,previewEpoch=0,batchInputs=[],batchCatalog=[],batchSelected=new Set(),batchActive='';
async function previewBatch(){
 const ticket=++previewEpoch;
 batchPreview=null;$('baseline-start').disabled=true;$('baseline-summary').textContent='正在计算所选范围…';$('baseline-start').textContent='正在预览…';
 try{
 if(!batchSelected.size)throw Error('请至少选择一个截点');
 const selection={split:baselineSplit,sample_ids:[...batchSelected]};
 const d=await post(`/api/baseline/preview/${encodeURIComponent(baselineKey)}`,selection);
 if(ticket!==previewEpoch)return;
 batchPreview={...selection,selection_hash:d.selection_hash};
 $('baseline-summary').textContent=`${d.host} · ${d.model} · ${d.count} 条 · 已确认 ${d.reviewed} 条；保持现有 .env 参数。`;
 batchInputs=d.samples;$('outbound-preview').textContent=JSON.stringify(d.samples.slice(0,10),null,2);
 $('baseline-start').textContent=`开始 ${d.count} 条评测`;
 $('baseline-status').textContent='';
 }catch(e){if(ticket===previewEpoch)$('baseline-status').textContent=e.message;}finally{if(ticket===previewEpoch)$('baseline-start').disabled=!batchPreview;}
}
$('batch-preview').onclick=previewBatch;
function invalidateBatch(){++previewEpoch;batchPreview=null;$('baseline-start').disabled=true;$('baseline-start').textContent='先预览所选样本';$('scope-confirm').checked=false;$('baseline-status').textContent='选择已更新，请预览后运行。';$('batch-count').textContent=`已选 ${batchSelected.size} / ${batchCatalog.reduce((n,g)=>n+g.samples.length,0)} 个截点`;}
function filteredBatch(){const q=$('batch-search').value.toLowerCase();return batchCatalog.map(g=>({...g,samples:g.samples.filter(s=>(g.id+' '+s.text).toLowerCase().includes(q))})).filter(g=>g.samples.length);}
function renderBatch(){
 const groups=filteredBatch();if(!groups.some(g=>g.id===batchActive))batchActive=groups[0]?.id||'';
 $('batch-groups').innerHTML=groups.map(g=>`<div class="batch-group"><input type="checkbox" aria-label="选择会话 ${esc(g.id)}" data-group="${esc(g.id)}"><button data-batch-group="${esc(g.id)}" class="${g.id===batchActive?'chosen':''}">${esc(g.id)} <small>${g.samples.filter(s=>batchSelected.has(s.id)).length}/${g.samples.length} 个截点</small></button></div>`).join('')||'<p>没有匹配会话</p>';
 $('batch-groups').querySelectorAll('[data-group]').forEach(el=>{const g=groups.find(g=>g.id===el.dataset.group),count=g.samples.filter(s=>batchSelected.has(s.id)).length;el.checked=count===g.samples.length;el.indeterminate=count>0&&count<g.samples.length;el.onchange=()=>{g.samples.forEach(s=>el.checked?batchSelected.add(s.id):batchSelected.delete(s.id));invalidateBatch();renderBatch();};});
 $('batch-groups').querySelectorAll('[data-batch-group]').forEach(b=>b.onclick=()=>{batchActive=b.dataset.batchGroup;renderBatch();});
 const group=groups.find(g=>g.id===batchActive);
 $('batch-samples').innerHTML=group?`<b>${esc(group.id)} · 勾选需要送测的截点</b>`+group.samples.map(s=>`<label class="batch-sample"><input type="checkbox" data-sample="${esc(s.id)}" ${batchSelected.has(s.id)?'checked':''}><span><b>第 ${s.seq} 轮 · ${s.reviewed?'已标注':'待标注'}</b><small>${esc(s.text)}</small></span></label>`).join(''):'<p>选择左侧会话</p>';
 $('batch-samples').querySelectorAll('[data-sample]').forEach(el=>el.onchange=()=>{el.checked?batchSelected.add(el.dataset.sample):batchSelected.delete(el.dataset.sample);invalidateBatch();renderBatch();});
 $('batch-count').textContent=`已选 ${batchSelected.size} / ${batchCatalog.reduce((n,g)=>n+g.samples.length,0)} 个截点`;
}
$('batch-search').oninput=renderBatch;
$('batch-all').onclick=()=>{filteredBatch().forEach(g=>g.samples.forEach(s=>batchSelected.add(s.id)));invalidateBatch();renderBatch();};
$('batch-clear').onclick=()=>{batchSelected.clear();invalidateBatch();renderBatch();};
$('batch-current').onclick=()=>{batchSelected=new Set((batchCatalog.find(g=>g.id===activeConversation)?.samples||[]).map(s=>s.id));batchActive=activeConversation;$('batch-search').value='';invalidateBatch();renderBatch();};
$('baseline-open').onclick=async()=>{
 if(demo){notice('请先返回真实数据。');return;}
 baselineKey=$('dataset').value;baselineSplit=$('split').value;
 batchCatalog=[];batchSelected.clear();$('batch-search').value='';$('baseline-summary').textContent='请选择会话或截点';
 invalidateBatch();renderBatch();$('baseline-dialog').showModal();
 try{await loadPromptLibrary();const d=await api(`/api/baseline/catalog/${encodeURIComponent(baselineKey)}?${new URLSearchParams({split:baselineSplit})}`);batchCatalog=d.groups;batchActive=activeConversation;renderBatch();}catch(e){$('baseline-status').textContent=e.message;}
};
$('baseline-start').onclick=async()=>{
 if(promptEditing||!$('prompt-select').value){$('baseline-status').textContent='请先保存自定义版本或选择已有版本。';return;}
 if(!batchPreview||!$('scope-confirm').checked){$('baseline-status').textContent='请预览范围并勾选调用确认。';return;}
 $('baseline-start').disabled=true;
 try{
 const d=await post(`/api/baseline/${encodeURIComponent(baselineKey)}`,{...batchPreview,prompt_id:$('prompt-select').value});
 $('split').value=baselineSplit;$('search').value='';$('errors').checked=false;
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

let annotationPackage=null,mergePlan=null,mergeKey='';
$('merge-close').onclick=()=>$('merge-dialog').close();
$('annotations-export').onclick=async()=>{
 if(demo)return notice('请先返回真实数据。');
 try{const key=$('dataset').value;const d=await api(`/api/annotations/export/${encodeURIComponent(key)}`);
 const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download=key+'-annotations.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 notice('已导出标注包；发给队友后，在相同数据版本下导入。不需要上传 GitHub。');
 }catch(e){notice(e.message);}
};
$('annotations-import').onclick=()=>{if(demo)return notice('请先返回真实数据。');$('annotations-file').click();};
$('annotations-file').onchange=async()=>{
 const f=$('annotations-file').files[0];if(!f)return;
 try{
 if(f.size>20*1024*1024)throw Error('标注包超过 20 MB');
 annotationPackage=JSON.parse(await f.text());mergeKey=$('dataset').value;
 mergePlan=await post(`/api/annotations/preview/${encodeURIComponent(mergeKey)}`,{package:annotationPackage});
 const n=mergePlan.counts;
 $('merge-summary').textContent=`新增 ${n.add} 条，更新 ${n.update} 条，保留 ${n.skip} 条，冲突 ${n.conflict} 条。此操作不调用模型。`;
 $('merge-conflicts').innerHTML=mergePlan.conflicts.map((c,i)=>`<div class="review-message"><b>${esc(c.sample_id)}</b><p>本地：${esc(JSON.stringify(c.local.gold))} · ${esc(c.local.reviewers.join('、'))}</p><p>导入：${esc(JSON.stringify(c.incoming.gold))} · ${esc(c.incoming.reviewers.join('、'))}</p><select id="merge-choice-${i}"><option value="">请选择</option><option value="local">保留本地</option><option value="incoming">采用导入（保留旧记录历史）</option></select></div>`).join('');
 $('merge-status').textContent='';$('merge-dialog').showModal();
 }catch(e){notice(e.message);}finally{$('annotations-file').value='';}
};
$('merge-apply').onclick=async()=>{
 $('merge-apply').disabled=true;
 try{
 const choices={};for(const [i,c] of mergePlan.conflicts.entries()){const v=$('merge-choice-'+i).value;if(!v)throw Error('请处理全部冲突');choices[c.sample_id]=v;}
 const d=await post(`/api/annotations/merge/${encodeURIComponent(mergeKey)}`,{package:annotationPackage,token:mergePlan.token,choices});
 $('merge-dialog').close();await load();notice(`已合并 ${d.changed} 条标注，保留 ${d.kept} 条。已有运行记录请点击“按最新复核重算”。`);
 }catch(e){$('merge-status').textContent=e.message;}finally{$('merge-apply').disabled=false;}
};

$('outbound-download').onclick=()=>{if(!batchPreview)return;const url=URL.createObjectURL(new Blob([JSON.stringify(batchInputs,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=baselineKey+'-outbound-preview.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

let promptEntries=[],promptEditing=false,promptPrevious='';
const promptSources={builtin:'内置',history:'历史运行',custom:'自定义'};
async function loadPromptLibrary(preferred=''){
 const d=await api('/api/prompts');promptEntries=d.entries;
 const chosen=preferred||$('prompt-select').value||d.default_id;
 $('prompt-select').innerHTML=promptEntries.map(e=>`<option value="${esc(e.id)}">${esc(e.version)} · ${promptSources[e.source]||esc(e.source)} · ${e.sha256.slice(0,8)}</option>`).join('');
 $('prompt-select').value=promptEntries.some(e=>e.id===chosen)?chosen:d.default_id;
 usePromptVersion();
}
function usePromptVersion(){
 const entry=promptEntries.find(e=>e.id===$('prompt-select').value);if(!entry)return;
 promptEditing=false;promptPrevious=entry.id;
 $('prompt-select').disabled=false;$('prompt-text').readOnly=true;$('prompt-text').value=entry.prompt;
 $('prompt-version').value=entry.version;$('prompt-name-field').hidden=true;$('prompt-save').hidden=true;$('prompt-cancel').hidden=true;
 $('prompt-info').textContent=`${promptSources[entry.source]}版本 · 已保存 · ${entry.sha256.slice(0,12)}`;
 $('prompt-status').textContent='';
}
function editPrompt(blank){
 if(!promptEntries.length)return;
 promptEditing=true;$('prompt-select').disabled=true;$('prompt-text').readOnly=false;
 $('prompt-name-field').hidden=false;$('prompt-save').hidden=false;$('prompt-cancel').hidden=false;
 $('prompt-version').value='';if(blank)$('prompt-text').value='';
 $('prompt-info').textContent='正在编辑新版本：保存后才可运行，原版本保持不变。';$('prompt-status').textContent='';$('prompt-version').focus();
}
$('prompt-select').onchange=usePromptVersion;
$('prompt-copy').onclick=()=>editPrompt(false);
$('prompt-new').onclick=()=>editPrompt(true);
$('prompt-cancel').onclick=()=>{$('prompt-select').value=promptPrevious;usePromptVersion();};
$('prompt-save').onclick=async()=>{
 $('prompt-save').disabled=true;
 try{const d=await post('/api/prompts',{version:$('prompt-version').value,prompt:$('prompt-text').value});await loadPromptLibrary(d.id);$('prompt-status').textContent='新版本已保存到本地，下次打开仍可选择。';}
 catch(e){$('prompt-status').textContent=e.message;}finally{$('prompt-save').disabled=false;}
};
