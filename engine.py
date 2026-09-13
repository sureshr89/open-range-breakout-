from __future__
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from functools import lru_cache
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
    def ready(self):
        return bool(self.client_id and self.access_token)

    def post(self, path, payload):
        if not self.ready:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        try:
            r = self.session.post(f"{API}{path}", json=payload, timeout=(3, 8))
        except requests.RequestException as exc:
            raise RuntimeError(f"Dhan request timed out/failed: {exc}") from exc
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Dhan returned HTTP {r.status_code} with non-JSON response")
        if r.status_code >= 400 or str(data.get("status", "")).lower() == "failure":
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data.get("errorMessage") or data))
        return data

    def ohlc(self, securities):
        return self.post("/marketfeed/ohlc", securities)

@lru_cache(maxsize=1)
def _cached_universe():
    r = requests.get(NIFTY500_URL, timeout=(3, 8), headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    f = pd.read_csv(io.BytesIO(r.content))
    cols = {re.sub(r"[^A-Z0-9]", "", str(c).upper()): c for c in f.columns}
    sym_col = cols.get("SYMBOL") or cols.get("SYMBOLNAME")
    if not sym_col:
        raise RuntimeError(f"NIFTY 500 list has no symbol column: {list(f.columns)[:20]}")
    return sorted({str(x).strip().upper() for x in f[sym_col].dropna() if str(x).strip()})

@lru_cache(maxsize=1)
def _cached_master():
    r = requests.get(DHAN_MASTER_URL, timeout=(3, 12), headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    f = pd.read_csv(io.BytesIO(r.content), low_memory=False, on_bad_lines="skip")
    original = list(f.columns)
    normalized = {re.sub(r"[^A-Z0-9]", "", str(c).upper()): c for c in f.columns}

    def choose(*names):
        for name in names:
            key = re.sub(r"[^A-Z0-9]", "", name.upper())
            if key in normalized:
                return normalized[key]
        for name in names:
            key = re.sub(r"[^A-Z0-9]", "", name.upper())
            for actual, original_name in normalized.items():
                if key in actual:
                    return original_name
        return None

    sid = choose("SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID", "SECURITY_ID", "SECURITYID")
    sym = choose("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "TRADING_SYMBOL", "TRADINGSYMBOL", "SYMBOLNAME")
    exch = choose("SEM_EXM_EXCH_ID", "SEM_EXCH_ID", "EXCHANGE_ID", "EXCHID", "EXCHANGE")
    if not sid or not sym:
        raise RuntimeError(f"Dhan master schema not recognized: {original[:30]}")
    if exch:
        f = f[f[exch].astype(str).str.upper().isin(["NSE", "NSE_EQ", "NSECM", "NSE CM"])]

    master = {}
    for _, row in f.iterrows():
        symbol = str(row.get(sym, "")).strip().upper()
        security = str(row.get(sid, "")).strip()
        if symbol in ("", "NAN", "NONE") or security in ("", "NAN", "NONE"):
            continue
        master.setdefault(symbol, {"symbol": symbol, "security_id": security})
    return master

class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(risk_per_trade, max_daily_loss, target_rr)
        self.cache_file = Path("orb_state.json")
        self.last_error = ""
        self._today = date.today()
        self._scan_cache = {"at": 0.0, "df": None}
        self.state = self._load_state()

    def _load_state(self):
        try:
            state = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {"positions": []}
        except Exception:
            state = {"positions": []}
        state.setdefault("positions", [])
        return state

    def daily_pnl(self):
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == str(self._today)))

    def _load_universe(self): return _cached_universe()
    def _load_master(self): return _cached_master()

    @staticmethod
    def _quote_map(data):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        return root.get("NSE_EQ", {}) if isinstance(root, dict) else {}

    def stock_scan(self):
        now = time_module.monotonic()
        if self._scan_cache["df"] is not None and now - self._scan_cache["at"] < 15:
            return self._scan_cache["df"]
        try:
            universe, master = self._load_universe(), self._load_master()
            configs = [master[s] for s in universe if s in master and str(master[s]["security_id"]).isdigit()]
            if not configs:
                raise RuntimeError("No NIFTY 500 NSE equity instruments matched Dhan master.")
            payload = {"NSE_EQ": [int(c["security_id"]) for c in configs]}
            quotes = self.dhan.ohlc(payload)
            qmap = self._quote_map(quotes)
            rows = []
            for c in configs:
                item = qmap.get(str(c["security_id"])) or qmap.get(int(c["security_id"]))
                if not item: continue
                o = item.get("ohlc") or {}
                ltp = item.get("last_price")
                if ltp in (None, ""): continue
                pdc = o.get("close")
                pdc_num = float(pdc) if pdc not in (None, "", 0) else None
                ltp_num = float(ltp)
                rows.append({"Symbol": c["symbol"], "LTP": ltp_num, "Open": o.get("open"), "PDC": pdc, "Today %": ((ltp_num-pdc_num)/pdc_num*100 if pdc_num else None), "1W %": None, "1M %": None, "3M %": None, "ORB High": None, "ORB Low": None, "Buy condition": "WAIT"})
            result = pd.DataFrame(rows)
            self.last_error = "" if not result.empty else "Dhan returned no NIFTY 500 quotes."
        except Exception as e:
            self.last_error = f"NIFTY 500 scan error: {type(e).__name__}: {e}"
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
