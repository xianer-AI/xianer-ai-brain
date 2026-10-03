"""Scoped Feishu API transport. Never emits credentials or response bodies on errors."""
import json,urllib.request,urllib.parse,urllib.error
from pathlib import Path
BASE='https://open.feishu.cn/open-apis'
def _resolve_secret(config, raw):
 if isinstance(raw,str) and raw:return raw
 if not isinstance(raw,dict) or raw.get('source')!='file':raise RuntimeError('unsupported Feishu secret reference')
 provider=config.get('secrets',{}).get('providers',{}).get(raw.get('provider'))
 if not isinstance(provider,dict) or provider.get('source')!='file' or provider.get('mode','singleValue')!='singleValue':raise RuntimeError('unsupported Feishu secret provider')
 value=Path(provider['path']).read_text().strip()
 if not value:raise RuntimeError('empty Feishu secret file')
 return value
def credentials(actor):
 if actor=='xiaowen':
  root=json.loads((Path.home()/'.openclaw/openclaw.json').read_text())
  c=root['channels']['feishu'];a=c['accounts']['main']
  return a.get('appId',c.get('appId')),_resolve_secret(root,a.get('appSecret',c.get('appSecret')))
 if actor=='yuanbao':
  values={}
  for line in (Path.home()/'.hermes/.env').read_text().splitlines():
   line=line.strip()
   if not line or line.startswith('#') or '=' not in line:continue
   key,value=line.split('=',1)
   values[key.strip()]=value.strip().strip('"').strip("'")
  return values['FEISHU_APP_ID'],values['FEISHU_APP_SECRET']
 raise ValueError('unknown actor')
def _http(method,url,data=None,token=None):
 headers={'Content-Type':'application/json'}
 if token:headers['Authorization']='Bearer '+token
 req=urllib.request.Request(BASE+url,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
 try:
  with urllib.request.urlopen(req,timeout=4) as r:p=json.load(r)
 except urllib.error.HTTPError as exc:
  # Feishu puts the actionable validation reason in the JSON error body.
  # Keep it bounded and exclude headers/tokens so delivery state is diagnosable.
  try: body=json.loads(exc.read().decode('utf-8','replace'))
  except Exception: body={}
  detail=body.get('msg') or body.get('error') or body.get('code')
  raise RuntimeError(f'Feishu HTTP {exc.code}: {detail}' if detail else f'Feishu HTTP {exc.code}') from exc
 if p.get('code',0)!=0:raise RuntimeError('Feishu API error code '+str(p.get('code')))
 return p
_tokens={}
def _token(actor):
 if actor not in _tokens:
  aid,secret=credentials(actor)
  _tokens[actor]=_http('POST','/auth/v3/tenant_access_token/internal',{'app_id':aid,'app_secret':secret})['tenant_access_token']
 return _tokens[actor]
def _invalid_token(error):
 text=str(error).lower()
 return 'invalid access token' in text or 'token expired' in text or '20005' in text
def request(actor,method,url,data=None):
 token=_token(actor)
 try:
  return _http(method,url,data,token)
 except RuntimeError as exc:
  if not _invalid_token(exc):
   raise
  # A tenant token can expire while this long-lived worker is running.
  # Refresh once, then retry the exact request; callers retain their normal
  # idempotency key and no second business operation is created.
  _tokens.pop(actor,None)
  return _http(method,url,data,_token(actor))
