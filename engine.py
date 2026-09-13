from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import io, json, re, time as time_module
import requests
import pandas as pd

API = "https://api.dhan.co/v2"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0

class DhanClient:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type":"application/json","Accept":"application/json","access-token":self.access_token,"client-id":self.client_id})
    @property
    def ready(self): return bool(self.client_id and self.access_token)
    def post(self, path, payload):
        if not self.ready: raise RuntimeError("Dhan Client ID and Access Token are required.")
        r = self.session.post(f"{API}{path}", json=payload, timeout=30)
        try: data = r.json()
        except Exception: r.raise_for_status(); raise RuntimeError(f"Dhan returned HTTP {r.status_code}")
        if r.status_code >= 400 or str(data.get("status","")).lower() == "failure":
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data.get("errorMessage") or data))
        return data
    def ohlc(self, securities): return self.post("/marketfeed/ohlc", securities)

class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(risk_per_trade, max_daily_loss, target_rr)
        self.cache_file = Path("orb_state.json")
        self.last_error = ""
        self._today = date.today()
        self._universe_cache = None
        self._master_cache = None
        self._scan_cache = {"at": 0.0, "df": None}
        self.state = self._load_state()
    def _load_state(self):
        try: state = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {"positions":[]}
        except Exception: state = {"positions":[]}
        state.setdefault("positions", [])
        return state
    def daily_pnl(self):
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == str(self._today)))
    def _load_universe(self):
        if self._universe_cache is not None: return self._universe_cache
        r = requests.get(NIFTY500_URL, timeout=30, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        f = pd.read_csv(io.BytesIO(r.content))
        cols = {re.sub(r"[^A-Z0-9]", "", str(c).upper()): c for c in f.columns}
        sym_col = cols.get("SYMBOL") or cols.get("SYMBOLNAME")
        if not sym_col: raise RuntimeError(f"NIFTY 500 list has no symbol column: {list(f.columns)[:20]}")
        self._universe_cache = sorted({str(x).strip().upper() for x in f[sym_col].dropna() if str(x).strip()})
        return self._universe_cache
    def _load_master(self):
        if self._master_cache is not None: return self._master_cache
        r = requests.get(DHAN_MASTER_URL, timeout=60, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        f = pd.read_csv(io.BytesIO(r.content), low_memory=False)
        original = list(f.columns)
        normalized = {re.sub(r"[^A-Z0-9]", "", str(c).upper()): c for c in f.columns}
        def choose(*names):
            for name in names:
                for key, original_name in normalized.items():
                    if name in key: return original_name
            return None
        sid = choose("SEMSECURITYID", "SECURITYID", "SECURITY_ID")
        sym = choose("SEMTRADINGSYMBOL", "TRADINGSYMBOL", "CUSTOMSYMBOL", "SYMBOLNAME", "SYMBOL")
        exch = choose("SEMEXCHID", "EXCHID", "EXCHANGEID", "EXCHANGE")
        if not sid or not sym:
            self.last_error = f"Dhan master schema not recognized: {original[:20]}"
            self._master_cache = {}
            return self._master_cache
        if exch:
            f = f[f[exch].astype(str).str.upper().str.contains("NSE", na=False)]
        master = {}
        for _, row in f.iterrows():
            symbol = str(row.get(sym, "")).strip().upper()
            security = str(row.get(sid, "")).strip()
            if symbol in ("", "NAN", "NONE") or security in ("", "NAN", "NONE"): continue
            if symbol not in master:
                master[symbol] = {"symbol": symbol, "security_id": security}
        self._master_cache = master
        return master
    @staticmethod
    def _quote_map(data):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        return root.get("NSE_EQ", {}) if isinstance(root, dict) else {}
    def stock_scan(self):
        now = time_module.monotonic()
        if self._scan_cache["df"] is not None and now - self._scan_cache["at"] < 15:
            return self._scan_cache["df"]
        try:
            universe = self._load_universe()
            master = self._load_master()
            configs = [master[s] for s in universe if s in master]
            if not configs: raise RuntimeError(self.last_error or "No NIFTY 500 NSE equity instruments matched Dhan master.")
            # Exactly one Dhan live-data request for the whole NIFTY 500 batch.
            payload = {"NSE_EQ": [int(c["security_id"]) for c in configs if str(c["security_id"]).isdigit()]}
            quotes = self.dhan.ohlc(payload)
            qmap = self._quote_map(quotes)
            rows = []
            for c in configs:
                item = qmap.get(str(c["security_id"])) or qmap.get(int(c["security_id"]))
                if not item: continue
                o = item.get("ohlc") or {}
                ltp = item.get("last_price")
                if ltp in (None, ""): continue
                op = o.get("open")
                pdc = o.get("close")
                rows.append({"Symbol": c["symbol"], "LTP": float(ltp), "Open": op, "PDC": pdc, "Today %": ((float(ltp)-float(pdc))/float(pdc)*100 if pdc not in (None, "", 0) else None), "1W %": None, "1M %": None, "3M %": None, "ORB High": None, "ORB Low": None, "Buy condition": "WAIT"})
            result = pd.DataFrame(rows)
            self.last_error = "" if not result.empty else "Dhan returned no NIFTY 500 quotes."
        except Exception as e:
            self.last_error = f"NIFTY 500 scan error: {e}"
            result = pd.DataFrame([{"Status": self.last_error}])
        self._scan_cache = {"at": now, "df": result}
        return result
    def setup_table(self, direction):
        f = self.stock_scan()
        if f.empty or "Buy condition" not in f.columns: return f
        return f[f["Buy condition"] == "BUY"].reset_index(drop=True) if direction == "BUY" else f[f["Buy condition"] != "BUY"].reset_index(drop=True)
    def today_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])
    def past_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])
    def strategy_markdown(self): return "**Universe:** NSE NIFTY 500. **Live data:** one Dhan NSE_EQ OHLC batch per 15-second refresh. **ORB/history:** shown when a separate cached candle source is added. Paper trading only."
    def config_table(self): return pd.DataFrame()
