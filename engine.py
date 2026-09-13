from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta, time
from pathlib import Path
import io, json, time as time_module
import requests
import pandas as pd

API = "https://api.dhan.co/v2"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

DEFAULT_CONFIG = {"NIFTY": {"security_id": "13", "exchange": "IDX_I", "instrument": "INDEX", "symbol": "NIFTY"}}

@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0

class DhanClient:
    def __init__(self, client_id: str, access_token: str):
        self.client_id, self.access_token = client_id.strip(), access_token.strip()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type":"application/json","Accept":"application/json","access-token":self.access_token,"client-id":self.client_id})
    @property
    def ready(self): return bool(self.client_id and self.access_token)
    def _post(self, path, payload):
        if not self.ready: raise RuntimeError("Dhan Client ID and Access Token are required.")
        r = self.session.post(f"{API}{path}", json=payload, timeout=20); r.raise_for_status(); data=r.json()
        if isinstance(data, dict) and data.get("status") == "failure": raise RuntimeError(data.get("remarks") or data.get("message") or str(data))
        return data
    def ltp(self, securities): return self._post("/marketfeed/ltp", securities)
    def daily(self, security_id, exchange, instrument, from_date, to_date):
        return self._post("/charts/historical", {"securityId":str(security_id),"exchangeSegment":exchange,"instrument":instrument,"expiryCode":0,"oi":False,"fromDate":from_date,"toDate":to_date})
    def intraday(self, security_id, exchange, instrument, interval, from_dt, to_dt):
        return self._post("/charts/intraday", {"securityId":str(security_id),"exchangeSegment":exchange,"instrument":instrument,"interval":interval,"oi":False,"fromDate":from_dt,"toDate":to_dt})

def _to_df(data):
    if not data or "timestamp" not in data: return pd.DataFrame()
    n=len(data["timestamp"]); out=pd.DataFrame({k:data.get(k,[None]*n) for k in ["timestamp","open","high","low","close","volume"]})
    out["timestamp"]=pd.to_datetime(out["timestamp"], unit="s", errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan=DhanClient(client_id, access_token); self.risk=RiskState(risk_per_trade,max_daily_loss,target_rr)
        self.cache_file=Path("orb_state.json"); self.config=dict(DEFAULT_CONFIG); self.last_error=""; self._today=date.today(); self._load_state()
        self._universe_cache=None; self._master_cache=None; self._stock_cache={}; self._scan_cache={"at":0.0,"df":None}
    def _load_state(self):
        try: self.state=json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {"positions":[]}
        except Exception: self.state={"positions":[]}
        self.state.setdefault("positions",[])
    def daily_pnl(self): return float(sum(float(p.get("pnl",0)) for p in self.state["positions"] if str(p.get("date"))==str(self._today)))
    def _cfg(self): return self.config["NIFTY"]
    def _nifty500_ltp(self):
        c=self._cfg(); q=self.dhan.ltp({c["exchange"]:[int(c["security_id"])]}); item=q.get("data",{}).get(c["exchange"],{}).get(str(c["security_id"])) or q.get("data",{}).get(c["exchange"],{}).get(int(c["security_id"]))
        if not item: raise RuntimeError("Dhan returned no NIFTY quote for securityId 13")
        return float(item["last_price"])
    def reference_levels(self, c):
        key=c["symbol"]
        if key in self._stock_cache and time_module.monotonic()-self._stock_cache[key][0] < 900: return self._stock_cache[key][1]
        d=_to_df(self.dhan.daily(c["security_id"],c["exchange"],c["instrument"],(self._today-timedelta(days=120)).strftime("%Y-%m-%d"),(self._today+timedelta(days=1)).strftime("%Y-%m-%d")))
        if d.empty: refs={"pdc":None,"week_close":None,"month_close":None,"quarter_close":None}
        else:
            x=d.close.astype(float).reset_index(drop=True); refs={"pdc":float(x.iloc[-2]) if len(x)>1 else None,"week_close":float(x.iloc[-6]) if len(x)>=6 else float(x.iloc[0]),"month_close":float(x.iloc[-22]) if len(x)>=22 else float(x.iloc[0]),"quarter_close":float(x.iloc[-66]) if len(x)>=66 else float(x.iloc[0])}
        self._stock_cache[key]=(time_module.monotonic(),refs); return refs
    def opening_range(self,c):
        d=_to_df(self.dhan.intraday(c["security_id"],c["exchange"],c["instrument"],"1",f"{self._today} 09:15:00",f"{self._today} 09:30:00")); d=d[(d.timestamp.dt.time>=time(9,15))&(d.timestamp.dt.time<time(9,30))]
        return None if d.empty else {"high":float(d.high.max()),"low":float(d.low.min())}
    def _load_universe(self):
        if self._universe_cache is not None: return self._universe_cache
        r=requests.get(NIFTY500_URL,timeout=20,headers={"User-Agent":"Mozilla/5.0"}); r.raise_for_status(); df=pd.read_csv(io.BytesIO(r.content))
        sym_col=next((c for c in df.columns if str(c).strip().lower() in ("symbol","symbol name")),None)
        if not sym_col: raise RuntimeError("NIFTY 500 constituent CSV has no Symbol column")
        self._universe_cache=[str(x).strip().upper() for x in df[sym_col].dropna().tolist()]
        return self._universe_cache
    def _load_master(self):
        if self._master_cache is not None: return self._master_cache
        r=requests.get(DHAN_MASTER_URL,timeout=45); r.raise_for_status(); df=pd.read_csv(io.BytesIO(r.content),low_memory=False)
        df.columns=[str(c).strip().upper() for c in df.columns]
        def col(*names): return next((n for n in names if n in df.columns),None)
        sym, sid, exch, seg, inst = col("SYMBOL_NAME","SYMBOL"),col("SECURITY_ID"),col("EXCH_ID"),col("SEGMENT"),col("INSTRUMENT")
        if not all((sym,sid,exch)): raise RuntimeError("Dhan instrument master format changed")
        df=df[(df[exch].astype(str).str.upper()=="NSE")]
        if seg: df=df[df[seg].astype(str).str.upper().isin(["E","C","NSE_EQ","EQUITY"]) | df[seg].isna()]
        self._master_cache={str(row[sym]).strip().upper():{"security_id":str(row[sid]),"exchange":"NSE_EQ","instrument":"EQUITY","symbol":str(row[sym]).strip().upper()} for _,row in df.iterrows()}
        return self._master_cache
    def stock_scan(self):
        if self._scan_cache["df"] is not None and time_module.monotonic()-self._scan_cache["at"] < 15: return self._scan_cache["df"]
        universe=self._load_universe(); master=self._load_master(); rows=[]
        for symbol in universe:
            c=master.get(symbol)
            if not c: continue
            try:
                refs=self.reference_levels(c)
                q=self.dhan.ltp({"NSE_EQ":[int(c["security_id"])]}); item=q.get("data",{}).get("NSE_EQ",{}).get(str(c["security_id"])) or q.get("data",{}).get("NSE_EQ",{}).get(int(c["security_id"]))
                if not item: continue
                ltp=float(item.get("last_price")); op=float(item.get("open",ltp)); pdc=refs.get("pdc")
                orr=self.opening_range(c); orh=orr.get("high") if orr else None; orl=orr.get("low") if orr else None
                rows.append({"Symbol":symbol,"LTP":ltp,"Open":op,"PDC":pdc,"Today %":((ltp-pdc)/pdc*100 if pdc else None),"1W %":((ltp-refs["week_close"])/refs["week_close"]*100 if refs.get("week_close") else None),"1M %":((ltp-refs["month_close"])/refs["month_close"]*100 if refs.get("month_close") else None),"3M %":((ltp-refs["quarter_close"])/refs["quarter_close"]*100 if refs.get("quarter_close") else None),"ORB High":orh,"ORB Low":orl,"Buy condition":("BUY" if orh is not None and ltp>orh else "WAIT")})
            except Exception: continue
        df=pd.DataFrame(rows); self._scan_cache={"at":time_module.monotonic(),"df":df}; return df
    def snapshot(self):
        r={"warning":None,"data_status":"Disconnected","daily_pnl":self.daily_pnl(),"pdc":None,"week_close":None,"month_close":None,"quarter_close":None,"nifty500_ltp":None,"nifty500_change":None,"nifty500_change_pct":None}
        if not self.dhan.ready: r["warning"]="Enter Dhan credentials in Streamlit Secrets."; return r
        try:
            c=self._cfg(); r["nifty500_ltp"]=self._nifty500_ltp(); r.update(self.reference_levels(c));
            if r["pdc"] is not None: r["nifty500_change"]=r["nifty500_ltp"]-r["pdc"]; r["nifty500_change_pct"]=r["nifty500_change"]/r["pdc"]*100 if r["pdc"] else None
            r["data_status"]="Connected to Dhan"
        except Exception as e: r["warning"]=f"Dhan data error: {e}"; self.last_error=str(e)
        return r
    def setup_table(self,direction):
        df=self.stock_scan()
        if df.empty: return pd.DataFrame([{"Status":"No stock data returned. Check Dhan credentials, NSE CSV access, and instrument master."}])
        if direction=="BUY": return df[df["Buy condition"]=="BUY"].sort_values(["Today %"],ascending=False).reset_index(drop=True)
        return df[df["Buy condition"]!="BUY"].sort_values(["Today %"],ascending=True).reset_index(drop=True)
    def market_table(self): return pd.DataFrame()
    def today_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))==str(self._today)])
    def past_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))!=str(self._today)])
    def strategy_markdown(self): return "**Universe:** NIFTY 500 constituents. **ORB:** 09:15–09:29 IST. **Buy condition:** LTP > ORB High. **Alignment columns:** Today % vs PDC, 1W %, 1M %, 3M %. This is a paper-trading scanner; no orders are placed."
    def config_table(self): return pd.DataFrame([{"Name":k,**v} for k,v in self.config.items()])
