from __future__ import annotations
import json, os, sqlite3, threading, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT=Path(__file__).parent
DB=Path(os.getenv('DATABASE_PATH',str(ROOT/'data'/'paper_trader.db')))
DB.parent.mkdir(parents=True,exist_ok=True)
IST=ZoneInfo('Asia/Kolkata')
BASE='https://www.nseindia.com'
HEAD={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36','Accept':'application/json,text/plain,*/*','Accept-Language':'en-US,en;q=0.9','Referer':'https://www.nseindia.com/option-chain'}
s=requests.Session(); lock=threading.Lock(); cache={'at':0,'data':None}
app=FastAPI(title='Option Chain AI Agent',version='0.1.0')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
def now(): return datetime.now(IST).isoformat(timespec='seconds')
def conn():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; return c
with conn() as c:
 c.execute('''CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,status TEXT,direction TEXT,symbol TEXT,strike REAL,expiry TEXT,quantity INTEGER,entry REAL,target REAL,stop REAL,exit REAL,pnl REAL,score REAL,reason TEXT,opened_at TEXT,closed_at TEXT,exit_reason TEXT,snapshot TEXT)''')
def clean_leg(x): return {k:x.get(k) for k in ('lastPrice','change','pChange','totalTradedVolume','openInterest','changeinOpenInterest','impliedVolatility','bidprice','askPrice','bidQty','askQty')}
def get_market():
 with lock:
  if cache['data'] and time.time()-cache['at']<4: return cache['data']
  try:
   s.get(BASE,headers=HEAD,timeout=8)
   r=s.get(BASE+'/api/option-chain-indices?symbol=NIFTY',headers=HEAD,timeout=8); r.raise_for_status()
   raw=r.json(); rec=raw.get('records',{}); rows=[]
   for x in rec.get('data',[]):
    if x.get('strikePrice') is not None and x.get('expiryDate'):
     rows.append({'strike':x['strikePrice'],'expiry':x['expiryDate'],'ce':clean_leg(x.get('CE') or {}),'pe':clean_leg(x.get('PE') or {})})
   if not rows: raise RuntimeError('NSE returned an empty option chain')
   d={'status':'LIVE','source':'NSE website public endpoint','source_timestamp':rec.get('timestamp') or raw.get('timestamp'),'received_at':now(),'spot':rec.get('underlyingValue'),'expiry_dates':rec.get('expiryDates',[]),'rows':rows,'underlying':'NIFTY'}
   cache.update(at=time.time(),data=d); return d
  except Exception as e:
   if cache['data']:
    d=dict(cache['data']); d.update(status='STALE',age_seconds=round(time.time()-cache['at']),error=str(e)); return d
   return {'status':'UNAVAILABLE','source':'NSE website public endpoint','source_timestamp':None,'received_at':now(),'spot':None,'expiry_dates':[],'rows':[],'error':str(e),'underlying':'NIFTY'}
def score_candidates(rows,atm):
 out=[]
 for row in rows:
  for side in ('CE','PE'):
   x=row[side.lower()]; vals=[x.get(k) for k in ('lastPrice','totalTradedVolume','openInterest','bidprice','askPrice')]
   if not all(isinstance(v,(int,float)) and v>0 for v in vals): continue
   ltp,vol,oi,bid,ask=vals; spread=(ask-bid)/ltp*100
   if spread>2: continue
   pct=x.get('pChange') or 0; change=x.get('change') or 0
   score=max(0,min(100,55+min(20,max(-20,pct))+(8 if change>0 else -5)+(8 if abs(row['strike']-atm)<=50 else 0)+(5 if spread<.75 else 0)))
   out.append({'direction':'BUY '+side,'strike':row['strike'],'expiry':row['expiry'],'ltp':ltp,'bid':bid,'ask':ask,'spread_pct':round(spread,2),'volume':vol,'oi':oi,'change_oi':x.get('changeinOpenInterest'),'iv':x.get('impliedVolatility'),'score':round(score,1),'status':'WATCH','reason':'Option-chain screen only. Spot and premium candle confirmation is not yet implemented; this is not an entry signal.'})
 return sorted(out,key=lambda z:z['score'],reverse=True)[:10]
@app.get('/')
def home(): return FileResponse(ROOT/'static'/'index.html')
@app.get('/api/health')
def health(): return {'ok':True,'time':now(),'data_status':cache.get('data',{}).get('status','NOT_FETCHED')}
@app.get('/api/market')
def market(): return get_market()
@app.get('/api/analysis')
def analysis():
 m=get_market()
 if m['status']!='LIVE' or m.get('spot') is None: return {'status':'WAIT','message':'Live NSE data unavailable or stale. Paper entries are paused.','market':m,'signals':[]}
 expiry=next((e for e in m['expiry_dates'] if any(r['expiry']==e for r in m['rows'])),None)
 rows=[r for r in m['rows'] if r['expiry']==expiry]; atm=round(float(m['spot'])/50)*50
 selected=sorted([r for r in rows if abs(float(r['strike'])-atm)<=500],key=lambda r:r['strike'])
 return {'status':'LIVE','market':m,'atm':atm,'expiry':expiry,'scan_count':len(selected),'signals':score_candidates(selected,atm)}
class OpenTrade(BaseModel):
 direction:str; strike:float; expiry:str; quantity:int=Field(default=1,ge=1,le=100); entry:float=Field(gt=0); score:float=Field(ge=0,le=100); reason:str=Field(min_length=3,max_length=1000); snapshot:dict={}
@app.get('/api/trades')
def trades():
 with conn() as c: return [dict(x) for x in c.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 500').fetchall()]
@app.post('/api/trades/open')
def open_trade(t:OpenTrade):
 if t.direction not in ('BUY CE','BUY PE'): raise HTTPException(400,'Invalid direction')
 if get_market()['status']!='LIVE': raise HTTPException(409,'Live NSE data required; entry paused')
 with conn() as c:
  if c.execute("SELECT 1 FROM trades WHERE status='OPEN'").fetchone(): raise HTTPException(409,'Only one open trade allowed')
  e=round(t.entry,2)
  cur=c.execute("INSERT INTO trades(status,direction,symbol,strike,expiry,quantity,entry,target,stop,score,reason,opened_at,snapshot) VALUES('OPEN',?,'NIFTY',?,?,?,?,?,?,?,?,?,?)",(t.direction,t.strike,t.expiry,t.quantity,e,e+20,e-10,t.score,t.reason,now(),json.dumps(t.snapshot)))
  return {'id':cur.lastrowid,'entry':e,'target':e+20,'stop':e-10}
class CloseTrade(BaseModel): exit:float=Field(gt=0); reason:str='MANUAL'
@app.post('/api/trades/{tid}/close')
def close_trade(tid:int,x:CloseTrade):
 with conn() as c:
  t=c.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'",(tid,)).fetchone()
  if not t: raise HTTPException(404,'Open trade not found')
  pnl=round((x.exit-t['entry'])*t['quantity'],2)
  c.execute("UPDATE trades SET status='CLOSED',exit=?,pnl=?,closed_at=?,exit_reason=? WHERE id=?",(x.exit,pnl,now(),x.reason,tid))
  return {'id':tid,'pnl':pnl}
@app.post('/api/trades/auto-check')
def auto_check():
 m=get_market()
 if m['status']!='LIVE': return {'checked':False,'reason':'Live data unavailable'}
 done=[]
 with conn() as c:
  for t in c.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall():
   row=next((r for r in m['rows'] if r['strike']==t['strike'] and r['expiry']==t['expiry']),None)
   if not row: continue
   ltp=(row['ce'] if t['direction']=='BUY CE' else row['pe']).get('lastPrice')
   if not isinstance(ltp,(int,float)) or ltp<=0: continue
   reason='TARGET' if ltp>=t['target'] else 'STOP LOSS' if ltp<=t['stop'] else None
   if reason:
    pnl=round((ltp-t['entry'])*t['quantity'],2)
    c.execute("UPDATE trades SET status='CLOSED',exit=?,pnl=?,closed_at=?,exit_reason=? WHERE id=?",(ltp,pnl,now(),reason,t['id']))
    done.append({'id':t['id'],'reason':reason,'exit':ltp,'pnl':pnl})
 return {'checked':True,'closed':done}
@app.get('/api/analytics')
def analytics():
 with conn() as c: rows=c.execute("SELECT * FROM trades WHERE status='CLOSED'").fetchall()
 wins=sum(1 for r in rows if (r['pnl'] or 0)>0); n=len(rows)
 return {'closed':n,'wins':wins,'losses':n-wins,'win_rate':round(wins/n*100,1) if n else 0,'net_pnl_before_costs':round(sum(r['pnl'] or 0 for r in rows),2),'target_hits':sum(r['exit_reason']=='TARGET' for r in rows),'stop_hits':sum(r['exit_reason']=='STOP LOSS' for r in rows),'warning':'Simulated P&L excludes brokerage, taxes, slippage and other charges; it does not predict future results.'}
