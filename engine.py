from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date, time as DTime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import io, json, logging, math, os, re, time
import pandas as pd
import requests

IST=ZoneInfo('Asia/Kolkata'); API='https://api.dhan.co/v2'
NIFTY500_URL='https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv'
MASTER_URL='https://images.dhan.co/api-data/api-scrip-master.csv'
QUOTE_LIMIT=1000; QUOTE_INTERVAL=3.2
ORB_START,ORB_END=DTime(9,15),DTime(9,30); ENTRY_END,FORCE_EXIT=DTime(13,0),DTime(14,55)
ORB_CANDIDATE_LIMIT=int(os.getenv('ORB_CANDIDATE_LIMIT','20'))

def clean(v):
 s=str(v or '').strip().upper(); return '' if s in {'','NAN','NONE','NULL'} else s
def norm(v): return re.sub(r'[^A-Z0-9]','',clean(v))
def num(v):
 try:
  x=float(v); return x if math.isfinite(x) else None
 except (TypeError,ValueError): return None
def today(): return datetime.now(IST).date()
def now(): return datetime.now(IST)
@dataclass
class RiskState:
 risk_per_trade:float=2250.; max_daily_loss:float=5000.; target_rr:float=2.; max_positions:int=3
class DhanClient:
 def __init__(self,client_id,token):
  self.client_id,self.token=clean(client_id),str(token or '').strip(); self.s=requests.Session(); self.s.headers.update({'Content-Type':'application/json','Accept':'application/json','access-token':self.token,'client-id':self.client_id}); self.last_call=0.; self.cooldown_until=0.
 def _post(self,path,payload):
  if not self.client_id or not self.token: raise RuntimeError('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')
  wait=max(QUOTE_INTERVAL-(time.monotonic()-self.last_call),self.cooldown_until-time.monotonic(),0)
  if wait: time.sleep(wait)
  for attempt in range(3):
   r=self.s.post(API+path,json=payload,timeout=(8,35)); self.last_call=time.monotonic()
   if r.status_code==429:
    pause=min(30*(attempt+1),90); self.cooldown_until=time.monotonic()+pause
    if attempt<2: time.sleep(pause); continue
    raise RuntimeError('Dhan rate limit (HTTP 429). Retried safely; wait before refreshing.')
   if r.status_code>=400: raise RuntimeError(f'Dhan HTTP {r.status_code}: {r.text[:300]}')
   data=r.json()
   if str(data.get('status','')).lower() in {'failure','failed','error'}: raise RuntimeError(str(data))
   return data
  raise RuntimeError('Dhan request failed')
 def ohlc(self,ids): return self._post('/marketfeed/ohlc',{'NSE_EQ':[int(x) for x in ids]})
 def intraday(self,sid,start,end): return self._post('/charts/intraday',{'securityId':str(sid),'exchangeSegment':'NSE_EQ','instrument':'EQUITY','interval':'1','oi':False,'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),'toDate':end.strftime('%Y-%m-%d %H:%M:%S')})
@lru_cache(maxsize=1)
def nifty_symbols():
 r=requests.get(NIFTY500_URL,headers={'User-Agent':'Mozilla/5.0'},timeout=(8,25)); r.raise_for_status(); f=pd.read_csv(io.BytesIO(r.content)); cols={norm(c):c for c in f.columns}; col=cols.get('SYMBOL') or cols.get('SYMBOLNAME');
 if not col: raise RuntimeError('NIFTY 500 CSV has no SYMBOL column')
 return sorted({clean(x) for x in f[col].dropna() if clean(x)})
@lru_cache(maxsize=1)
def dhan_master():
 r=requests.get(MASTER_URL,headers={'User-Agent':'Mozilla/5.0'},timeout=(8,60)); r.raise_for_status(); f=pd.read_csv(io.BytesIO(r.content),low_memory=False,on_bad_lines='warn'); cols={norm(c):c for c in f.columns}
 def find(*ns):
  for n in ns:
   if norm(n) in cols:return cols[norm(n)]
 sid=find('SEM_SMST_SECURITY_ID','SEM_SECURITY_ID','SECURITY_ID','SECURITYID'); trade=find('SEM_TRADING_SYMBOL','TRADING_SYMBOL','TRADINGSYMBOL'); custom=find('SEM_CUSTOM_SYMBOL','CUSTOM_SYMBOL','SYMBOLNAME'); exch=find('SEM_EXM_EXCH_ID','EXCHANGE_ID','EXCHID','EXCHANGE')
 if not sid or not(trade or custom): raise RuntimeError('Unrecognized Dhan master schema')
 eq={}
 for row in f.itertuples(index=False):
  d=row._asdict(); symbol=clean(d.get(trade,'')) if trade else ''; symbol=symbol or (clean(d.get(custom,'')) if custom else ''); security=clean(d.get(sid,'')); exchange=clean(d.get(exch,'')) if exch else ''
  if symbol and security.isdigit() and (not exchange or exchange in {'NSE','NSE_EQ','NSECM'}): eq.setdefault(symbol.split('-')[0],{'symbol':symbol,'security_id':security,'exchange':'NSE_EQ','instrument':'EQUITY'})
 return eq
def _bucket(data):
 d=data.get('data',{}) if isinstance(data,dict) else {}; return d.get('NSE_EQ',{}) if isinstance(d,dict) else {}
def _q(bucket,sid): return bucket.get(str(sid)) or bucket.get(int(sid)) or {}
def _candles(data):
 d=data.get('data',data) if isinstance(data,dict) else {}
 if not isinstance(d,dict) or not all(k in d for k in ['timestamp','open','high','low','close','volume']): return pd.DataFrame()
 keys=['timestamp','open','high','low','close','volume']; n=min(len(d[k]) for k in keys); f=pd.DataFrame({k:d[k][:n] for k in keys}); f['timestamp']=pd.to_datetime(f.timestamp,unit='s',utc=True,errors='coerce').dt.tz_convert(IST)
 for k in keys[1:]: f[k]=pd.to_numeric(f[k],errors='coerce')
 return f.dropna(subset=['timestamp','high','low','close'])
class ORBEngine:
 def __init__(self,client_id,access_token,risk_per_trade=2250,max_daily_loss=5000,target_rr=2):
  self.dhan=DhanClient(client_id,access_token); self.risk=RiskState(float(risk_per_trade),float(max_daily_loss),float(target_rr)); self.state_file=Path(os.getenv('ORB_STATE_FILE','orb_state.json')); self.orb_file=Path(os.getenv('ORB_RANGE_FILE',f'orb_ranges_{today().isoformat()}.json')); self.state=self._load(); self.last_error=''; self.last_refresh=None; self.cache={'at':0,'df':None}; self.orb_cache=self._load_orb()
 def _load(self):
  try:x=json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
  except Exception:x={}
  x.setdefault('positions',[]); return x
 def _save(self):
  t=self.state_file.with_suffix('.tmp'); t.write_text(json.dumps(self.state,indent=2)); t.replace(self.state_file)
 def _load_orb(self):
  try:return json.loads(self.orb_file.read_text()) if self.orb_file.exists() else {}
  except Exception:return {}
 def _save_orb(self):
  t=self.orb_file.with_suffix('.tmp'); t.write_text(json.dumps(self.orb_cache,indent=2)); t.replace(self.orb_file)
 def daily_pnl(self):return sum(num(p.get('pnl')) or 0 for p in self.state['positions'] if p.get('date')==today().isoformat() and p.get('closed'))
 def unrealized_pnl(self):return sum(num(p.get('unrealized')) or 0 for p in self.state['positions'] if p.get('date')==today().isoformat() and not p.get('closed'))
 def can_trade(self):return self.daily_pnl()>-self.risk.max_daily_loss and sum(1 for p in self.state['positions'] if p.get('date')==today().isoformat() and not p.get('closed'))<self.risk.max_positions
 def _orb(self,item):
  key=str(item['security_id'])
  if key in self.orb_cache:return self.orb_cache[key]
  d=today(); f=_candles(self.dhan.intraday(item['security_id'],datetime.combine(d,ORB_START,tzinfo=IST),datetime.combine(d,ORB_END,tzinfo=IST))); f=f[(f.timestamp.dt.time>=ORB_START)&(f.timestamp.dt.time<ORB_END)]
  if len(f)<10:return None
  v={'high':float(f.high.max()),'low':float(f.low.min()),'bars':int(len(f))}; self.orb_cache[key]=v; self._save_orb(); return v
 def _update_positions(self,prices):
  changed=False
  for p in self.state['positions']:
   if p.get('date')!=today().isoformat() or p.get('closed') or p.get('symbol') not in prices:continue
   ltp=prices[p['symbol']]; side=p['side']; reason=None
   if side=='BUY' and ltp<=p['stop']:reason='STOP'
   elif side=='BUY' and ltp>=p['target']:reason='TARGET'
   elif side=='SELL' and ltp>=p['stop']:reason='STOP'
   elif side=='SELL' and ltp<=p['target']:reason='TARGET'
   elif now().time()>=FORCE_EXIT:reason='FORCE_EXIT'
   p['unrealized']=(ltp-p['entry'])*p['quantity'] if side=='BUY' else (p['entry']-ltp)*p['quantity']
   if reason:p.update(exit=ltp,exit_time=now().isoformat(),reason=reason,pnl=p['unrealized'],closed=True,unrealized=0); changed=True
  if changed:self._save()
 def _enter(self,row,orb):
  if not self.can_trade() or any(p.get('symbol')==row['Symbol'] and p.get('date')==today().isoformat() for p in self.state['positions']):return
  entry=row['LTP']; side=row['Signal']; stop=orb['low'] if side=='BUY' else orb['high']; dist=abs(entry-stop)
  if not entry or not stop or dist<=0:return
  qty=max(1,int(self.risk.risk_per_trade//dist)); target=entry+dist*self.risk.target_rr if side=='BUY' else entry-dist*self.risk.target_rr
  self.state['positions'].append({'date':today().isoformat(),'time':now().isoformat(),'symbol':row['Symbol'],'side':side,'entry':entry,'stop':stop,'target':target,'quantity':qty,'pnl':0,'closed':False}); self._save()
 def stock_scan(self):
  if self.cache['df'] is not None and time.monotonic()-self.cache['at']<15:return self.cache['df']
  try:
   items=[dhan_master()[n] for n in nifty_symbols() if n in dhan_master()]; rows=[]
   for i in range(0,len(items),QUOTE_LIMIT):
    batch=items[i:i+QUOTE_LIMIT]; bucket=_bucket(self.dhan.ohlc([x['security_id'] for x in batch]))
    for item in batch:
     q=_q(bucket,item['security_id']); o=q.get('ohlc') or {}; ltp=num(q.get('last_price') or q.get('ltp')); pdc=num(o.get('close'))
     if ltp is not None: rows.append({'Symbol':item['symbol'],'Security ID':item['security_id'],'LTP':ltp,'Open':num(o.get('open')),'High':num(o.get('high')),'Low':num(o.get('low')),'PDC':pdc,'Today %':((ltp-pdc)/pdc*100 if pdc else None),'Signal':'WAIT'})
   df=pd.DataFrame(rows); prices={r['Symbol']:r['LTP'] for r in rows}; self._update_positions(prices)
   if now().time()>=ORB_END and now().time()<=ENTRY_END and not df.empty:
    cand=df.dropna(subset=['Today %']).assign(_rank=lambda x:x['Today %'].abs()).nlargest(ORB_CANDIDATE_LIMIT,'_rank')
    for idx in cand.index:
     item=next((x for x in items if x['symbol']==df.at[idx,'Symbol']),None)
     if not item:continue
     orb=self._orb(item)
     if orb:
      ltp=df.at[idx,'LTP']; sig='BUY' if ltp>orb['high'] else ('SELL' if ltp<orb['low'] else 'WAIT'); df.at[idx,'Signal']=sig; df.at[idx,'ORB High']=orb['high']; df.at[idx,'ORB Low']=orb['low']
      if sig in {'BUY','SELL'}:self._enter(df.loc[idx].to_dict(),orb)
   self.last_error=''; self.last_refresh=now(); self.cache={'at':time.monotonic(),'df':df}; return df
  except Exception as e:self.last_error=f'{type(e).__name__}: {e}'; return self.cache['df'] if self.cache['df'] is not None else pd.DataFrame({'Status':[self.last_error]})
 def index_metrics(self):return pd.DataFrame()
 def setup_table(self,direction):
  f=self.stock_scan(); return f[f.get('Signal',pd.Series(dtype=str))==direction].reset_index(drop=True) if 'Signal' in f else f
 def today_positions(self):return pd.DataFrame([p for p in self.state['positions'] if p.get('date')==today().isoformat()])
 def past_positions(self):return pd.DataFrame([p for p in self.state['positions'] if p.get('date')!=today().isoformat()])
 def strategy_markdown(self):return '**ORB rules:** 09:15–09:30 IST range; entries 09:30–13:00; force exit 14:55. Paper trading only.'
 def config_table(self):return pd.DataFrame([{'Setting':'ORB','Value':'09:15–09:30 IST'},{'Setting':'Entry window','Value':'09:30–13:00 IST'},{'Setting':'Force exit','Value':'14:55 IST'},{'Setting':'Orders','Value':'Disabled / paper only'}])
