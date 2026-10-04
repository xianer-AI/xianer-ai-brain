"""Conservative weather safety reminder for Datang, Zhuji; silent on ordinary days."""
import datetime as dt, json, sqlite3, urllib.parse, urllib.request, uuid
from pathlib import Path
from zoneinfo import ZoneInfo
from router import STATE, GROUP
TZ=ZoneInfo('Asia/Shanghai'); LAT,LON=29.70,120.23
URL='https://api.open-meteo.com/v1/forecast?'+urllib.parse.urlencode({'latitude':LAT,'longitude':LON,'daily':'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code','timezone':'Asia/Shanghai','forecast_days':1})
def forecast():
 with urllib.request.urlopen(URL,timeout=8) as r:return json.load(r)['daily']
def message(d):
 high=float(d['temperature_2m_max'][0]); low=float(d['temperature_2m_min'][0]); rain=int(d['precipitation_probability_max'][0] or 0); code=int(d['weather_code'][0])
 risk=[]
 if high>=35:risk.append(f'最高气温预计{high:.0f}℃，注意补水、防暑和适当休息')
 if low<=10:risk.append(f'最低气温预计{low:.0f}℃，尤其是夜班注意保暖')
 if rain>=60:risk.append(f'预计降雨概率约{rain}% ，进出厂区注意防滑')
 if code in {95,96,99}:risk.append('预计有雷电风险，设备和出行按现场安全安排执行')
 if not risk:return None
 return '天气参考｜浙江省诸暨市大唐镇\n'+'；'.join(risk)+'。天气预报仅供参考，请以现场实际情况为准。'
def deliver(db=None,send=None,now=None):
 now=(now or dt.datetime.now(TZ)).astimezone(TZ); db=Path(db or STATE/'weather.sqlite')
 if not 7<=now.hour<10:return {'status':'outside_window'}
 text=message(forecast());
 if not text:return {'status':'ordinary_day'}
 db.parent.mkdir(parents=True,exist_ok=True)
 with sqlite3.connect(db) as c:
  c.execute('CREATE TABLE IF NOT EXISTS reminders(day TEXT PRIMARY KEY,status TEXT,receipt TEXT)')
  if c.execute('SELECT 1 FROM reminders WHERE day=?',(str(now.date()),)).fetchone():return {'status':'already_attempted'}
  c.execute('INSERT INTO reminders VALUES(?,?,?)',(str(now.date()),'attempting',''));c.commit()
 receipt=send(text,str(uuid.uuid5(uuid.NAMESPACE_URL,GROUP+':weather:'+str(now.date()))) if send else '') if send else ''
 with sqlite3.connect(db) as c:c.execute('UPDATE reminders SET status=?,receipt=? WHERE day=?',('sent',str(receipt),str(now.date())))
 return {'status':'sent','text':text}
if __name__=='__main__':print(json.dumps(deliver(),ensure_ascii=False))
