from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dtime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
import io, json, logging, math, re, time

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
API = "https://api.dhan.co/v2"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
QUOTE_LIMIT = 1000
QUOTE_INTERVAL = 1.05
CACHE_SECONDS = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orb")


def norm(v): return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
def clean(v):
    s = str(v or "").strip().upper()
    return "" if s in {"", "NAN", "NONE", "NULL"} else s

def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError): return None

@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0
    max_positions: int = 3

class DhanClient:
    def __init__(self, client_id, token):
        self.client_id, self.token = clean(client_id), str(token or "").strip()
        self.s = requests.Session()
        self.s.headers.update({"Content-Type":"application/json","Accept":"application/json","access-token":self.token,"client-id":self.client_id})
        self.last_call = 0.0
        self.cooldown = 0.0
    def ohlc(self, payload):
        if not self.client_id or not self.token: raise RuntimeError("Missing Dhan secrets")
        if time.monotonic() < self.cooldown: raise RuntimeError("Dhan quote cooldown active after HTTP 429")
        wait = QUOTE_INTERVAL - (time.monotonic() - self.last_call)
        if wait > 0: time.sleep(wait)
        r = self.s.post(f"{API}/marketfeed/ohlc", json=payload, timeout=(5,25)); self.last_call=time.monotonic()
        if r.status_code == 429: self.cooldown=time.monotonic()+60; raise RuntimeError("Dhan HTTP 429: retry after 60 seconds")
        if r.status_code >= 400: raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:300]}")
        try: data=r.json()
        except Exception: raise RuntimeError("Dhan returned non-JSON response")
        if isinstance(data,dict) and str(data.get("status","")).lower() in {"failure","failed","error"}: raise RuntimeError(str(data))
        return data

@lru_cache(maxsize=1)
def nifty_symbols():
    r=requests.get(NIFTY500_URL,headers={"User-Agent":"Mozilla/5.0"},timeout=(5,20)); r.raise_for_status()
    f=pd.read_csv(io.BytesIO(r.content)); cols={norm(c):c for c in f.columns}; c=cols.get("SYMBOL") or cols.get("SYMBOLNAME")
    if not c: raise RuntimeError(f"NIFTY CSV missing SYMBOL column: {list(f.columns)}")
    return sorted({clean(x) for x in f[c].dropna() if clean(x)})

@lru_cache(maxsize=1)
def dhan_master():
    r=requests.get(MASTER_URL,headers={"User-Agent":"Mozilla/5.0"},timeout=(5,40)); r.raise_for_status()
    f=pd.read_csv(io.BytesIO(r.content),low_memory=False,on_bad_lines="skip"); cols={norm(c):c for c in f.columns}
    def find(*names):
        for n in names:
            if norm(n) in cols:return cols[norm(n)]
        return None
    sid=find("SEM_SMST_SECURITY_ID","SEM_SECURITY_ID","SECURITY_ID","SECURITYID")
    trade=find("SEM_TRADING_SYMBOL","TRADING_SYMBOL","TRADINGSYMBOL")
    custom=find("SEM_CUSTOM_SYMBOL","CUSTOM_SYMBOL","SYMBOLNAME")
    seg=find("SEM_SEGMENT","SEGMENT"); exch=find("SEM_EXM_EXCH_ID","EXCHANGE_ID","EXCHID","EXCHANGE")
    inst=find("SEM_INSTRUMENT_NAME","INSTRUMENT_NAME","INSTRUMENT")
    if not sid or not (trade or custom): raise RuntimeError(f"Unrecognized Dhan master columns: {list(f.columns)[:30]}")
    eq={}; indices=[]
    for _,r in f.iterrows():
        t=clean(r.get(trade,"")) if trade else ""; c=clean(r.get(custom,"")) if custom else ""; symbol=t or c; security=clean(r.get(sid,""))
        if not symbol or not security.isdigit(): continue
        item={"symbol":symbol,"trading":t,"custom":c,"security_id":security,"segment":clean(r.get(seg,"")) if seg else "","exchange":clean(r.get(exch,"")) if exch else "","instrument":clean(r.get(inst,"")) if inst else ""}
        text=norm(" ".join(item.values())); isidx="INDEX" in text or "NIFTY500" in text or norm(symbol)=="NIFTY500"
        if isidx: indices.append(item)
        elif not exch or item["exchange"] in {"NSE","NSE_EQ","NSECM"}: eq.setdefault(symbol,item)
    return eq,indices

def quote(root,sid):
    if not isinstance(root,dict): return {}
    return root.get(str(sid)) or root.get(int(sid)) or {}

def root(data,segment):
    d=data.get("data",{}) if isinstance(data,dict) else {}; return d.get(segment,{}) if isinstance(d,dict) else {}

class ORBEngine:
    def __init__(self,client_id,access_token,risk_per_trade=2250,max_daily_loss=5000,target_rr=2):
        self.dhan=DhanClient(client_id,access_token); self.risk=RiskState(float(risk_per_trade),float(max_daily_loss),float(target_rr)); self.state_file=Path("orb_state.json"); self.last_error=""; self.cache={"at":0,"df":None,"index":None}; self.state=self._load()
    def _load(self):
        try: x=json.loads(self.state_file.read_text()) if self.state_file.exists() else {"positions":[]}
        except Exception: x={"positions":[]}
        if not isinstance(x,dict): x={"positions":[]}
        x.setdefault("positions",[]); return x
    def _save(self): self.state_file.write_text(json.dumps(self.state,indent=2,default=str))
    def daily_pnl(self): return sum(num(p.get("pnl")) or 0 for p in self.state["positions"] if str(p.get("date"))==str(date.today()))
    def can_trade(self): return self.daily_pnl()>-self.risk.max_daily_loss and sum(1 for p in self.state["positions"] if p.get("date")==str(date.today()) and not p.get("closed"))<self.risk.max_positions
    def paper_trade(self,symbol,side,entry,stop,target,quantity):
        if not self.can_trade(): return False
        if any(p.get("symbol")==symbol and p.get("date")==str(date.today()) and not p.get("closed") for p in self.state["positions"]): return False
        self.state["positions"].append({"date":str(date.today()),"time":datetime.now(IST).isoformat(),"symbol":symbol,"side":side,"entry":entry,"stop":stop,"target":target,"quantity":quantity,"pnl":0,"closed":False}); self._save(); return True
    def _scan_batch(self,items):
        out=[]
        for i in range(0,len(items),QUOTE_LIMIT):
            batch=items[i:i+QUOTE_LIMIT]; out.append(self.dhan.ohlc({"NSE_EQ":[int(x["security_id"]) for x in batch]}));
        return out
    def stock_scan(self):
        if self.cache["df"] is not None and time.monotonic()-self.cache["at"]<CACHE_SECONDS:return self.cache["df"]
        try:
            names=nifty_symbols(); eq,indices=dhan_master(); items=[eq[x] for x in names if x in eq]
            if not items: raise RuntimeError("No NIFTY 500 NSE equities matched Dhan master")
            rows=[]
            for response in self._scan_batch(items):
                bucket=root(response,"NSE_EQ")
                for item in items:
                    q=quote(bucket,item["security_id"]); o=q.get("ohlc") or {}; ltp=num(q.get("last_price") or q.get("ltp")); close=num(o.get("close"))
                    if ltp is None and close is None: continue
                    ltp=ltp or close; rows.append({"Symbol":item["symbol"],"Security ID":item["security_id"],"LTP":ltp,"Open":num(o.get("open")),"High":num(o.get("high")),"Low":num(o.get("low")),"PDC":close,"Today %":((ltp-close)/close*100) if close else None,"ORB High":None,"ORB Low":None,"Signal":"WAIT","Buy condition":"WAIT"})
            if not rows: raise RuntimeError("Dhan returned no NSE_EQ quotes")
            self.last_error=""; self.cache={"at":time.monotonic(),"df":pd.DataFrame(rows),"index":None}; return self.cache["df"]
        except Exception as e:
            self.last_error=f"{type(e).__name__}: {e}"; return self.cache["df"] if self.cache["df"] is not None else pd.DataFrame([{"Status":self.last_error}])
    def index_metrics(self): self.stock_scan(); return self.cache.get("index")
    def setup_table(self,direction):
        f=self.stock_scan(); return f[f.get("Signal",pd.Series(dtype=str))==direction].reset_index(drop=True) if "Signal" in f else f
    def today_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))==str(date.today())])
    def past_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))!=str(date.today())])
    def strategy_markdown(self): return "**ORB:** 09:15–09:30 IST opening range. Entry window 09:30–13:00. Long above ORB high; short below ORB low. Stop is opposite range; target = risk × configured R:R. Paper trading only."
    def config_table(self):
        try:
            _,idx=dhan_master(); return pd.DataFrame(idx)
        except Exception as e:return pd.DataFrame([{"Status":str(e)}])
