import json,sys
from transport import request
from router import GROUP
from weather import deliver
def send(text,key):
 p=request('yuanbao','POST','/im/v1/messages?receive_id_type=chat_id',{'receive_id':GROUP,'msg_type':'text','content':json.dumps({'text':text},ensure_ascii=False),'uuid':key})
 return p['data']['message_id']
print(json.dumps(deliver(send=send),ensure_ascii=False))
