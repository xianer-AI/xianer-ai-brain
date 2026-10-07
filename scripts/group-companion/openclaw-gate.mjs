import { execFile } from 'node:child_process';
import { startUploadAgent, verifyUploadReceipt, markUploadDispatchFailed, markUploadDispatchStarted, summarizeUploadFailure } from 'file:///Users/xianer/.openclaw/workspace/scripts/group-companion/upload-dispatch.mjs';
const GROUP='oc_1f8587b1bcde12a0d1bb6053ab2b748a';
const PY='/Users/xianer/.hermes/hermes-agent/venv/bin/python';
const SCRIPT='/Users/xianer/.openclaw/workspace/scripts/group-companion/router.py';
const STATUS='/Users/xianer/.openclaw/workspace/scripts/production-parallel/batch_status.py';
function inTargetGroup(event,ctx){
 const refs=[ctx?.conversationId,ctx?.sessionKey,event?.conversationId,event?.sessionKey].filter(Boolean).map(String);
 return ctx?.channelId==='feishu' && ctx?.accountId==='main' &&
   (refs.includes(GROUP) || refs.some(value=>value.includes(GROUP)));
}
export function lookup(payload){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,[SCRIPT],{timeout:25000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end(JSON.stringify(payload));
 });
}
export async function gate(event,ctx,route=lookup,api=null,status=runStatus){
 const cardResult=await cardAction(event,ctx,runCard);
 if(cardResult)return cardResult;
 if(!inTargetGroup(event,ctx))return;
 const content=String(event.content||'').trim();
 const backfill=await backfillChoice(event,ctx);
 if(backfill)return backfill;
 const template=await backfillTemplate(event,ctx);
 if(template)return template;
 if(content==='/new'){
  return {handled:true,text:'当前不支持 /new 命令。请直接发送生产数量，或回复“准确”确认当前核对卡。'};
 }
 if (looksLikeReport(content)) {
  return {handled:true,text:'收到，辛苦了！这批生产数据正在生成核对清单；历史批次状态不会影响本批处理。'};
 }
 if (isStatusRequest(content) && !looksLikeReport(content)) {
  try {
   const current=await status({sender:event.senderId||ctx.senderId,group:GROUP});
   return {handled:true,text:renderStatus(current)};
  } catch (error) {
   console.error('[production-status]',String(error?.message||error));
   return {handled:true,text:'生产状态暂时无法核实，请稍后再试。'};
  }
 }
 if(/^(?:准确|确认|确认上传)(?:\s+om_[A-Za-z0-9_-]+)?$/.test(content)){
  let bound=false;
  let confirmation=null;
  try{
   confirmation=await runCard({text:content,id:event.messageId||ctx.messageId,sender:event.senderId||ctx.senderId,group:GROUP});
   bound=true;
   const source=String(confirmation.source||'');
   const confirmationId=confirmation.callback||ctx.messageId||event.messageId;
   if(confirmation.already_dispatched)return {handled:true,text:'这批生产数据已经上传并完成核验，不会重复计量。'};
   if(confirmation.already_processing)return {handled:true,text:'这批生产数据已经收到，正在补偿处理中，不会重复计量。'};
   const claimed=await claimConfirmationDispatch(confirmationId);
   if(!claimed.claimed)return {handled:true,text:'已收到“准确”，正在核验并处理这批数据，请勿重复报数；上传成功后会发送回执。'};
   let workerSettled=false;
   const pid=await startUploadAgent({source,confirmation:confirmationId,group:GROUP},{
    onError:async(error,output)=>{
     if(workerSettled)return;
     workerSettled=true;
     const detail=summarizeUploadFailure(null,null,{...output,
      stderr:output?.stderr||error?.stderr||error?.message||String(error)});
     try{await markUploadDispatchFailed({confirmation:confirmationId,error:detail});}catch{}
     try{await recordConfirmationFailure({text:content,id:confirmationId,sender:event.senderId||ctx.senderId,group:GROUP,error:detail});}catch{}
    },
    onExit:async(code,signal,output)=>{
     if(workerSettled)return;
     workerSettled=true;
     if(code===0){
       try{
        await verifyUploadReceipt({source,confirmation:confirmationId});
        const marked=await markConfirmationDispatched(confirmationId, output?.replyMessageId);
        const report=output?.receipt?.report;
        // Every successful report goes through the same advancement hook.
        // Normal reports are harmless here (there is no matching scheduled
        // backfill row); choices 1/2 must also advance queued dates after
        // their zero-quantity review is uploaded.
        if(report?.worker && report?.production_date){
         try{await advanceBackfill(report.worker,report.production_date,output?.replyMessageId);}catch(error){
          console.error('[backfill-advance]',String(error?.message||error));
         }
        }
        return marked;
      }catch(error){
       const detail=summarizeUploadFailure(code,signal,{...output,
        stderr:[output?.stderr,error?.stderr||error?.message||String(error)].filter(Boolean).join('\n')});
       try{await markUploadDispatchFailed({confirmation:confirmationId,error:detail});}catch{}
       try{await recordConfirmationFailure({text:content,id:confirmationId,sender:event.senderId||ctx.senderId,group:GROUP,error:detail});}catch{}
       return;
      }
     }
     const detail=summarizeUploadFailure(code,signal,output);
     try{await markUploadDispatchFailed({confirmation:confirmationId,error:detail});}catch{}
     try{await recordConfirmationFailure({text:content,id:confirmationId,sender:event.senderId||ctx.senderId,group:GROUP,error:detail});}catch{}
    }
   });
   try{await markUploadDispatchStarted({confirmation:confirmationId,pid});}catch{}
   return {handled:true,text:'收到“准确”，辛苦了！正在处理这批生产数据；上传完成后会返回 GitHub commit 和回读结果。'};
  }catch(error){
   const detail=String(error?.stderr||error?.message||error||'未知错误');
   if(bound){
    try{await markUploadDispatchFailed({confirmation:confirmation.callback||event.messageId||ctx.messageId,error:detail});}catch{}
   }
   try{await recordConfirmationFailure({text:content,id:event.messageId||ctx.messageId,sender:event.senderId||ctx.senderId,group:GROUP,error:detail});}catch{}
   console.error('[production-confirmation]',JSON.stringify({messageId:event.messageId||ctx.messageId,sender:event.senderId||ctx.senderId,group:GROUP,error:detail}));
   const text=detail.includes('没有本人待确认')||detail.includes('核对卡')
    ? '未找到你本人当前有效的待确认核对卡，请先等待最新核对清单。'
    : detail.includes('待核实')
      ? `这张核对卡还有待核实项，暂不能上传：${detail.replace(/，请补齐后重新生成核对卡，不能直接上传$/, '')}`
    : detail.includes('运行时不可用')
      ? '已收到“准确”并完成批次绑定，但上传任务当前未启动成功，管理员需要检查生产任务运行状态。'
      : '已收到“准确”，但处理这批数据时发生异常，尚未上传；管理员需要检查运行日志。';
   return {handled:true,text};
  }
 }
 if(/^\s*(?:[ABCD]\s*[=:：]\s*(?:徐超超|梅芳|李鸿玉|张小翠)|(?:徐超超|梅芳|李鸿玉|张小翠)\s*[=:：]\s*[ABCD])\s*$/.test(content)){
  try{
   const r=await runIdentity({mode:'self_bind',text:content,id:event.messageId||ctx.messageId,sender:event.senderId||ctx.senderId,group:GROUP});
   return {handled:true,text:`已自动绑定 ${r.bound}=${r.name}。以后直接发送生产数量即可。`};
  }catch{return {handled:true,text:'自动绑定未完成：该账号或员工代号可能已经绑定到其他对象，请联系小文处理。'};}
 }
 if(/^绑定员工 [ABCD] om_[A-Za-z0-9_-]+$/.test(content)){
  try{const r=await runIdentity({text:content,id:event.messageId||ctx.messageId});return {handled:true,text:`已按老板确认绑定 ${r.bound} ${r.name} 的飞书账号。`};}
  catch{return {handled:true,text:'身份绑定未完成。现在可让员工本人在本群发送“B=梅芳”或“D=张小翠”这类声明自动绑定。'};}
 }
 try{
  const r=await route({group:GROUP,id:event.messageId||ctx.messageId});
  if(r.delivery==='queue'||r.owner!=='xiaowen')return {handled:true};
 }catch{return {handled:true}; /* Inbox retained; avoid two speakers on uncertain state. */}
}

export function isStatusRequest(content){
 return /(?:生产|报数|上传|同步).*(?:状态|进度|更新|完成|处理)|(?:跟进|查一下|看一下).*(?:生产|报数|上传|同步)|为什么.*(?:没|未|还).*(?:更新|上传|处理)|(?:\d{1,2}号?\s*(?:和|跟|、)\s*)?\d{1,2}号?.*(?:已|都|已经)?更新了|不需要再更新|之前.*更新/.test(content);
}
export function looksLikeReport(content){
 const text=String(content||'');
 // A clear read-only lookup can quote products and quantities. Keep it out
 // of the report acknowledgement path, without swallowing explicit reports.
 if (/^\s*(?:小文[，,\s]*)?(?:请|麻烦)?(?:帮我)?(?:查一下|查询|查看|查下|看一下|看下)/.test(text)
     && /(?:产量|产能|记录|入账|数量|总量|报表|汇总|统计)/.test(text)
     && !/(?:报数|补报|上报|更正|纠正|撤销)/.test(text)) return false;
 // Product names plus numbers also occur in capacity questions (e.g.
 // “小腿袜：1台、冰冰袜：8台”). Those are read-only analysis requests.
 if (/(?:统计|汇总|分析|产能|历史记录|平均|机台|机器|每款|按.*为准)/.test(text)
     && !/(?:报数|补报|上报|更正|纠正|撤销)/.test(text)) return false;
 // A production report must carry an explicit report marker or a complete
 // employee/date/process context; a free-form product example is not enough.
 const explicitReport=/(?:报数|补报|上报|更正|纠正|生产日|生产日期|工序|下机|烤边|代报)/.test(text);
 const sixProducts=(text.match(/(?:棉堆堆袜|冰冰袜|小腿袜|过膝袜|女船袜|男船袜)/g)||[]).length;
 return /(?:棉堆堆袜|冰冰袜|小腿袜|过膝袜|女船袜|男船袜)/.test(text)
   && /\d/.test(text) && (explicitReport || sixProducts>=6);
}
export function renderStatus(status){
 if(!status)return '目前没有需要你确认的生产批次。';
 const prefix=status.production_date?`${status.production_date}这批`:'这批';
 const version=status.workbench_version?`（生产统计工作台 ${status.workbench_version}）`:'';
 if(status.state==='completed')return `${prefix}已完成，系统已确认并完成 GitHub 回读。${version}`;
 if(status.state==='reply_pending')return `${prefix}GitHub 已验证，正在等待飞书成功回执确认，暂不重复上传。${version}`;
 if(status.state==='awaiting_confirmation')return `${prefix}正在等待核对，请核对后回复“准确”。${version}`;
 if(status.state==='confirmed')return `${prefix}已收到确认，系统正在自动同步。${version}`;
 if(status.state==='upload_failed')return `${prefix}${status.reason||'已确认，上传失败，系统已保留记录，管理员处理中'}。${version}`;
 if(status.state==='needs_reconciliation')return `${prefix}状态需要先核实，系统不会重复发送或重复上传。${version}`;
 return `${prefix}当前状态：${status.reason||'正在处理中'}。${version}`;
}

export async function cardAction(event,ctx,run){
 const text=String(event.content||'').trim();
 const callbackId=String(event.messageId||ctx.messageId||'');
 const isBackfillPayload=/^生产补报 [1-3] [ABCD] 20\d{2}-\d{2}-\d{2}$/.test(text);
 if(!inTargetGroup(event,ctx)||(!callbackId.startsWith('card-action-')&&!isBackfillPayload))return;
 if(isBackfillPayload)return backfillChoice(event,ctx);
 if(!/^生产核对 (confirm|modify|defer) [a-f0-9]{12}$/.test(text))return;
 try{
  const result=await run({text,id:event.messageId||ctx.messageId,sender:event.senderId||ctx.senderId,group:GROUP});
  if(result.action==='confirm')return; // Native model receives authenticated click; guard validates persisted receipt.
  const quantityNote=result.action==='modify' && result.quantity_note?`\n${result.quantity_note}`:'';
  return {handled:true,text:result.action==='modify'?`收到更正，谢谢说明！这批先不上传，请发送更正后的完整数量，我会重新给你核对。${quantityNote}`:'收到，谢谢说明！这批已暂缓上传，需要时再重新核对。'};
 }catch{return {handled:true,text:'旧核对按钮已停用。请直接回复“准确”，或回复“修改数量”并写出正确数量。'};}
}
export function backfillChoice(event,ctx){
 const text=String(event.content||'').trim();
 const payloadChoice=/^生产补报 [1-3] [ABCD] 20\d{2}-\d{2}-\d{2}$/.test(text);
 const visibleButton=/^(?:[1-3](?:\s+(?:当天未上班|已经报过|需要补报))?|当天未上班|已经报过|需要补报|补报)$/.test(text);
 const callbackId=String(event.messageId||ctx.messageId||'');
 // Feishu normally prefixes native card callbacks with card-action-. Some
 // clients deliver the exact button value without that prefix; accept only
 // the fully-qualified production payload in that case, never a bare 1/2/3.
 if(!inTargetGroup(event,ctx) || (!callbackId.startsWith('card-action-') && !payloadChoice && !visibleButton))return;
 if(!/^(?:[1-3]|补报|需要补报|当天未上班|已经报过|1 当天未上班|2 已经报过|3 需要补报)$/.test(text) && !payloadChoice)return;
 return new Promise((resolve,reject)=>{
  const args=['/Users/xianer/.openclaw/workspace/scripts/production-parallel/backfill_flow.py','choice',
    '--message-id',String(event.messageId||ctx.messageId),'--sender',String(event.senderId||ctx.senderId),'--text',text];
  const p=execFile(PY,args,{timeout:20000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{const result=JSON.parse(out);resolve(result&&result.handled?result:undefined);}catch(e){reject(e);}
  });
 });
}
export function backfillTemplate(event,ctx){
 const text=String(event.content||'').trim();
 if(!inTargetGroup(event,ctx) || !text.includes('补报') || !text.includes('生产日期'))return;
 if(!/(?:棉堆堆袜|冰冰袜|小腿袜|过膝袜|女船袜|男船袜)/.test(text))return;
 return new Promise((resolve,reject)=>{
  const args=['/Users/xianer/.openclaw/workspace/scripts/production-parallel/backfill_flow.py','template',
   '--message-id',String(event.messageId||ctx.messageId),'--sender',String(event.senderId||ctx.senderId),'--text',text];
  execFile(PY,args,{timeout:20000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{const result=JSON.parse(out);resolve(result&&result.handled?result:undefined);}catch(e){reject(e);}
  });
 });
}
export function runCard(payload){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,['/Users/xianer/.openclaw/workspace/scripts/production-parallel/review_cards.py','action'],{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end(JSON.stringify(payload));
 });
}
export function recordConfirmationFailure(payload){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,['/Users/xianer/.openclaw/workspace/scripts/production-parallel/review_cards.py','record-failed'],{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end(JSON.stringify(payload));
 });
}
export function markConfirmationDispatched(messageId,replyMessageId){
 return new Promise((resolve,reject)=>{
  const args=['/Users/xianer/.openclaw/workspace/scripts/production-parallel/review_cards.py','mark-dispatched','--message-id',messageId];
  if(replyMessageId)args.push('--reply-message-id',String(replyMessageId));
  const p=execFile(PY,args,{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end();
 });
}
export function claimConfirmationDispatch(messageId){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,['/Users/xianer/.openclaw/workspace/scripts/production-parallel/review_cards.py','claim-dispatch','--message-id',messageId],{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end();
 });
}
export function runIdentity(payload){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,['/Users/xianer/.openclaw/workspace/scripts/production-parallel/worker_identity.py'],{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });p.stdin.end(JSON.stringify(payload));
 });
}
export function runStatus(payload){
 return new Promise((resolve,reject)=>{
  const p=execFile(PY,[STATUS,'latest','--sender',payload.sender,'--group',payload.group],{timeout:15000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);
   try{resolve(JSON.parse(out||'null'));}catch(e){reject(e);}
  });
  p.stdin.end();
 });
}
export function advanceBackfill(worker,productionDate,receiptMessageId){
 return new Promise((resolve,reject)=>{
  const args=['/Users/xianer/.openclaw/workspace/scripts/production-parallel/backfill_flow.py','advance','--worker',worker,'--completed-date',productionDate];
  if(receiptMessageId)args.push('--receipt-message-id',String(receiptMessageId));
  const p=execFile(PY,args,{timeout:20000,maxBuffer:65536},(err,out)=>{
   if(err)return reject(err);try{resolve(JSON.parse(out));}catch(e){reject(e);}
  });
  p.stdin.end();
 });
}
