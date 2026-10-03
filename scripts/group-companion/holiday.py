"""Daytime festival greeting, one attempt per festival/day; no makeup-workday greetings."""
import datetime,json,sqlite3,uuid,sys
from pathlib import Path
from zoneinfo import ZoneInfo
from lunardate import LunarDate
from router import STATE,GROUP
TZ=ZoneInfo('Asia/Shanghai')
def festival(day):
 fixed={(1,1):'元旦',(5,1):'劳动节',(10,1):'国庆节'}
 if (day.month,day.day) in fixed:return fixed[(day.month,day.day)]
 lunar=LunarDate.fromSolarDate(day.year,day.month,day.day)
 if not lunar.isLeapMonth:
  name={(1,1):'春节',(5,5):'端午节',(8,15):'中秋节'}.get((lunar.month,lunar.day))
  if name:return name
 # Qingming falls at 15 degrees solar longitude; this standard 20th/21st-century formula includes 2008 correction.
 y=day.year%100;c=4.81 if day.year>=2000 else 5.59
 q=int(y*.2422+c)-int(y/4)+(1 if day.year==2008 else 0)
 if (day.month,day.day)==(4,q):return '清明节'
 return None
TEXT={
 '元旦':'新年好！小文和元宝祝大家新的一年平安顺心，生活有甜头，忙碌有收获。今天也记得好好吃饭、照顾自己！',
 '春节':'给大家拜年啦！小文和元宝祝大家新春快乐、家人安康，日子越过越红火。无论今天在忙还是休息，都愿你身边有暖意、心里有盼头！',
 '清明节':'清明时节，愿大家平安顺遂，也愿思念有所寄托。小文和元宝提醒大家留意天气，出行注意安全，照顾好自己和家人。',
 '劳动节':'劳动节快乐！每一份认真都值得被看见。小文和元宝祝大家忙有所获、闲有所乐，今天记得给自己留一点放松的时间。',
 '端午节':'端午安康！小文和元宝祝大家身体健康、生活顺心。粽子甜咸各有喜欢，咱们的祝福都是满满的；忙碌之余也记得好好吃饭。',
 '中秋节':'中秋快乐！小文和元宝祝大家平安喜乐、家人安康。月饼可以分着吃，好心情也欢迎分给群里的伙伴们。愿今天的你心里有团圆、身边有温暖。',
 '国庆节':'国庆快乐！小文和元宝祝大家平安顺心、生活多一点轻松和开心。无论今天在岗位上还是和家人相聚，都记得照顾好自己！'}
def deliver(db,now,send):
 now=now.astimezone(TZ)
 if not 9<=now.hour<12:return {'status':'outside_window'}
 name=festival(now.date())
 if not name:return {'status':'ordinary_day'}
 key=f'{GROUP}:{now.date()}:{name}'
 Path(db).parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(db,timeout=8) as c:
  c.execute('CREATE TABLE IF NOT EXISTS greetings(key TEXT PRIMARY KEY,status TEXT,receipt TEXT)')
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT 1 FROM greetings WHERE key=?',(key,)).fetchone():return {'status':'already_attempted','festival':name}
  c.execute('INSERT INTO greetings VALUES(?,?,?)',(key,'attempting',''));c.commit()
 receipt=send(TEXT[name],str(uuid.uuid5(uuid.NAMESPACE_URL,key)))
 with sqlite3.connect(db) as c:c.execute('UPDATE greetings SET status=?,receipt=? WHERE key=?',('sent',str(receipt),key))
 return {'status':'sent','festival':name,'receipt':receipt}
def send(text,key):
 from transport import request
 p=request('xiaowen','POST','/im/v1/messages?receive_id_type=chat_id',{'receive_id':GROUP,'msg_type':'text','content':json.dumps({'text':text},ensure_ascii=False),'uuid':key})
 return p['data']['message_id']
if __name__=='__main__':
 if '--send-if-due' not in sys.argv:
  now=datetime.datetime.now(TZ);print(json.dumps({'date':str(now.date()),'festival':festival(now.date()),'dry_run':True},ensure_ascii=False))
 else:print(json.dumps(deliver(STATE/'holidays.sqlite',datetime.datetime.now(TZ),send),ensure_ascii=False))
