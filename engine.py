from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta, time
from pathlib import Path
import json
import requests
import pandas as pd

API = "https://api.dhan.co/v2"

# Dhan IDX_I security id 390 is not a valid NIFTY 500 chart/quote id for this API.
# Use the supported NIFTY index id 13 and label it accurately.
DEFAULT_CONFIG = {
    "NIFTY": {"security_id": "13", "exchange": "IDX_I", "instrument": "INDEX", "symbol": "NIFTY"},
}

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
        self.dhan=DhanClient(client_id, access_token); self.risk=RiskState(risk_per_trade,max_daily_loss,target_rr); self.cache_file=Path("orb_state.json"); self.config=dict(DEFAULT_CONFIG); self.last_error=""; self._today=date.today(); self._load_state()
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
        d=_to_df(self.dhan.daily(c["security_id"],c["exchange"],c["instrument"],(self._today-timedelta(days=120)).strftime("%Y-%m-%d"),(self._today+timedelta(days=1)).strftime("%Y-%m-%d")))
        if d.empty: return {"pdc":None,"week_close":None,"month_close":None,"quarter_close":None}
        x=d.close.astype(float).reset_index(drop=True); return {"pdc":float(x.iloc[-2]) if len(x)>1 else None,"week_close":float(x.iloc[-6]) if len(x)>=6 else float(x.iloc[0]),"month_close":float(x.iloc[-22]) if len(x)>=22 else float(x.iloc[0]),"quarter_close":float(x.iloc[-66]) if len(x)>=66 else float(x.iloc[0])}
    def opening_range(self,c):
        d=_to_df(self.dhan.intraday(c["security_id"],c["exchange"],c["instrument"],"1",f"{self._today} 09:15:00",f"{self._today} 09:30:00")); d=d[(d.timestamp.dt.time>=time(9,15))&(d.timestamp.dt.time<time(9,30))]
        return None if d.empty else {"high":float(d.high.max()),"low":float(d.low.min()),"start":d.timestamp.iloc[0],"end":d.timestamp.iloc[-1]}
    def snapshot(self):
        r={"warning":None,"data_status":"Disconnected","daily_pnl":self.daily_pnl(),"pdc":None,"week_close":None,"month_close":None,"quarter_close":None,"nifty500_ltp":None,"nifty500_change":None,"nifty500_change_pct":None}
        if not self.dhan.ready: r["warning"]="Enter Dhan credentials in Streamlit Secrets."; return r
        try:
            c=self._cfg(); r["nifty500_ltp"]=self._nifty500_ltp(); r.update(self.reference_levels(c));
            if r["pdc"] is not None:
                r["nifty500_change"]=r["nifty500_ltp"]-r["pdc"]
                r["nifty500_change_pct"]=(r["nifty500_change"]/r["pdc"])*100 if r["pdc"] else None
            r["data_status"]="Connected to Dhan"
        except Exception as e: r["warning"]=f"Dhan data error: {e}"; self.last_error=str(e)
        return r
    def market_table(self):
        try:
            c=self._cfg(); l=self._nifty500_ltp(); refs=self.reference_levels(c); p=refs["pdc"]; pct=((l-p)/p*100) if p else None
            return pd.DataFrame([{"Instrument":c["symbol"],"LTP":l,"PDC":p,"Today % vs PDC":pct,"ORB High":None,"ORB Low":None}])
        except Exception:
            return pd.DataFrame([{"Instrument":"NIFTY","LTP":None,"PDC":None,"Today % vs PDC":None,"ORB High":None,"ORB Low":None}])
    def setup_table(self,direction):
        return pd.DataFrame([{"Symbol":"NIFTY","Direction":direction,"LTP":None,"ORB High":None,"ORB Low":None,"Signal":"WAIT","Filters":"Stock scanner not enabled"}])
    def today_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))==str(self._today)])
    def past_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date"))!=str(self._today)])
    def strategy_markdown(self): return "**Opening range:** 09:15–09:29 IST. **Entries:** 09:30–13:00 IST. **Force exit:** 14:55 IST. **NIFTY market filter:** only the current day's percentage change versus previous-day close is used (> 0 for bullish, < 0 for bearish). **Stock alignments:** the 1D/1W/1M/3M alignment rules belong to stock setups, not the NIFTY benchmark. Paper trading only."
    def config_table(self): return pd.DataFrame([{"Name":k,**v} for k,v in self.config.items()])
