from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time as DTime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import io, json, math, os, re, time
import pandas as pd
import requests

IST=ZoneInfo('Asia/Kolkata'); API='https://api.dhan.co/v2'
NIFTY500_URL='https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv'
MASTER_URL='https://images.dhan.co/api-data/api-scrip-master.csv'
QUOTE_LIMIT=1000; QUOTE_TTL=15; COOLDOWN=90
ORB_START,ORB_END=DTime(9,15),DTime(9,30); ENTRY_END,FORCE_EXIT=DTime(13,0),DTime(14,55)
ORB_CANDIDATE_LIMIT=int(os.getenv('ORB_CANDIDATE_LIMIT','20'))

def clean(v):
    s=str(v or '').strip().upper(); return '' if s in {'','NAN','NONE','NULL'} else s
def norm(v): return re.sub(r'[^A-Z0-9]','',clean(v))
def num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except (TypeError,ValueError): return None
def now(): return datetime.now(IST)
def today(): return now().date()
def market_open():
    d=now(); return d.weekday()<5 and DTime(9,15)<=d.time()<=DTime(15,30)

@dataclass
class RiskState:
    risk_per_trade:float=2250.; max_daily_loss:float=5000.; target_rr:float=2.; max_positions:int=3

class DhanClient:
    def __init__(self,client_id,token):
        self.client_id,self.token=clean(client_id),str(token or '').strip(); self.s=requests.Session(); self.s.headers.update({'Content-Type':'application/json','Accept':'application/json','access-token':self.token,'client-id':self.client_id}); self.last_call=0.; self.cooldown_until=0.
    def _post(self,path,payload):
        if not market_open(): raise RuntimeError('NSE market closed — live Dhan requests are disabled.')
        if not self.client_id or not self.token: raise RuntimeError('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')
        remaining=self.cooldown_until-time.monotonic()
        if remaining>0: raise RuntimeError(f'Dhan cooldown active for {int(remaining)}s after HTTP 429')
        wait=max(0.25-(time.monotonic()-self.last_call),0)
        if wait: time.sleep(wait)
        r=self.s.post(API+path,json=payload,timeout=(8,35)); self.last_call=time.monotonic()
        if r.status_code==429:
            self.cooldown_until=time.monotonic()+COOLDOWN
            raise RuntimeError('Dhan rate limit (HTTP 429). Dhan requests paused for 90 seconds.')
        if r.status_code>=400: raise RuntimeError(f'Dhan HTTP {r.status_code}: {r.text[:300]}')
        data=r.json()
        if str(data.get('status','')).lower() in {'failure','failed','error'}: raise RuntimeError(str(data))
        return data
    def ohlc(self,ids): return self._post('/marketfeed/ohlc',{'NSE_EQ':[int(x) for x in ids]})
    def intraday(self,sid,start,end): return self._post('/charts/intraday',{'securityId':str(sid),'exchangeSegment':'NSE_EQ','instrument':'EQUITY','interval':'1','oi':False,'fromDate':start.strftime('%Y-%m-%d %H:%M:%S'),'toDate':end.strftime('%Y-%m-%d %H:%M:%S')})

@lru_cache(maxsize=1)
def nifty_symbols():
    r=requests.get(NIFTY500_URL,headers={'User-Agent':'Mozilla/5.0'},timeout=(8,25)); r.raise_for_status(); f=pd.read_csv(io.BytesIO(r.content)); cols={norm(c):c for c in f.columns}; col=cols.get('SYMBOL') or cols.get('SYMBOLNAME')
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
        self.dhan=DhanClient(client_id,access_token); self.risk=RiskState(float(risk_per_trade),float(max_daily_loss),float(target_rr)); self.state_file=Path(os.getenv('ORB_STATE_FILE','orb_state.json')); self.state=self._load(); self.last_error=''; self.last_refresh=None; self.last_successful_quote=None; self.cache={'at':0,'df':None}; self.orb_cache={}
    def _load(self):
        try: x=json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        except Exception: x={}
        x.setdefault('positions',[]); return x
    def _save(self):
        t=self.state_file.with_suffix('.tmp'); t.write_text(json.dumps(self.state,indent=2)); t.replace(self.state_file)
    def daily_pnl(self): return sum(num(p.get('pnl')) or 0 for p in self.state['positions'] if p.get('date')==today().isoformat() and p.get('closed'))
    def can_trade(self): return self.daily_pnl()>-self.risk.max_daily_loss and sum(1 for p in self.state['positions'] if p.get('date')==today().isoformat() and not p.get('closed'))<self.risk.max_positions
    def stock_scan(self):
        if not market_open(): return pd.DataFrame({'Status':['NSE market closed — live Dhan requests are disabled.']})
        if self.cache['df'] is not None and time.monotonic()-self.cache['at']<QUOTE_TTL: return self.cache['df']
        try:
            master=dhan_master(); items=[master[n] for n in nifty_symbols() if n in master]; rows=[]
            for i in range(0,len(items),QUOTE_LIMIT):
                batch=items[i:i+QUOTE_LIMIT]; bucket=_bucket(self.dhan.ohlc([x['security_id'] for x in batch]))
                for item in batch:
                    q=_q(bucket,item['security_id']); o=q.get('ohlc') or {}; ltp=num(q.get('last_price') or q.get('ltp')); pdc=num(o.get('close'))
                    if ltp is not None: rows.append({'Symbol':item['symbol'],'Security ID':item['security_id'],'LTP':ltp,'Open':num(o.get('open')),'High':num(o.get('high')),'Low':num(o.get('low')),'PDC':pdc,'Today %':((ltp-pdc)/pdc*100 if pdc else None),'Signal':'WAIT','Source':'Dhan'})
            df=pd.DataFrame(rows); self._update_positions(df); self.last_successful_quote=now(); self.last_refresh=now(); self.last_error=''; self.cache={'at':time.monotonic(),'df':df}; return df
        except Exception as e:
            self.last_error=f'{type(e).__name__}: {e}'; return self.cache['df'] if self.cache['df'] is not None else pd.DataFrame({'Status':[self.last_error]})
    def _update_positions(self,df):
        if df.empty or 'Symbol' not in df: return
        prices=dict(zip(df['Symbol'],df['LTP']))
        changed=False
        for p in self.state['positions']:
            if p.get('closed'): continue
            ltp=prices.get(p.get('Symbol'))
            if ltp is None: continue
            p['LTP']=float(ltp); p['unrealized_pnl']=(float(ltp)-float(p.get('Entry',ltp)))*float(p.get('Qty',0))*(1 if p.get('Side')=='BUY' else -1); changed=True
            if now().time()>=FORCE_EXIT: p['closed']=True; p['Exit']=float(ltp); p['pnl']=p['unrealized_pnl']; p['exit_reason']='Force exit 14:55'; changed=True
        if changed: self._save()
    def index_metrics(self): return None
    def setup_table(self,direction):
        f=self.stock_scan()
        return f[f.get('Signal',pd.Series(dtype=str))==direction].reset_index(drop=True) if 'Signal' in f else f
    def today_positions(self): return pd.DataFrame([p for p in self.state['positions'] if p.get('date')==today().isoformat()])
    def past_positions(self): return pd.DataFrame([p for p in self.state['positions'] if p.get('date')!=today().isoformat()])
    def unrealized_pnl(self): return sum(num(p.get('unrealized_pnl')) or 0 for p in self.state['positions'] if not p.get('closed'))
    def strategy_markdown(self): return '**ORB rules:** 09:15–09:30 IST range; entries 09:30–13:00; force exit 14:55. Paper trading only. Live order placement is disabled.'
    def config_table(self): return pd.DataFrame([{'Setting':'ORB','Value':'09:15–09:30 IST'},{'Setting':'Entry window','Value':'09:30–13:00 IST'},{'Setting':'Force exit','Value':'14:55 IST'},{'Setting':'Orders','Value':'Disabled / paper only'},{'Setting':'Quote cache','Value':'15 seconds; shared response'}])
