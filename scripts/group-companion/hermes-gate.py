"""Local Hermes plugin: apply shared arbitration before model dispatch."""
import json,subprocess
GROUP='oc_1f8587b1bcde12a0d1bb6053ab2b748a'
SCRIPT='/Users/xianer/.openclaw/workspace/scripts/group-companion/router.py'
PYTHON='/Users/xianer/.hermes/hermes-agent/venv/bin/python'
def gate(event,**kwargs):
 source=event.source
 if getattr(source.platform,'value',source.platform)!='feishu' or source.chat_id!=GROUP:return
 if getattr(source,'is_bot',False):return {'action':'skip','reason':'companion_no_bot_loops'}
 try:
  p=subprocess.run([PYTHON,SCRIPT],input=json.dumps({'group':GROUP,'id':event.message_id}),text=True,capture_output=True,timeout=25,check=True)
  result=json.loads(p.stdout)
  if result['owner']=='yuanbao' and result.get('delivery')!='queue':return {'action':'allow'}
 except Exception:pass
 return {'action':'skip','reason':'companion_other_speaker_or_quiet'}
def register(ctx):ctx.register_hook('pre_gateway_dispatch',gate)
