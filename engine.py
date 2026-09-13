from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import io
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

API = "https://api.dhan.co/v2"
NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0


class DhanClient:
    def __init__(self, client_id, access_token):
        self.client_id = str(client_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json", "access-token": self.access_token, "client-id": self.client_id})

    def ohlc(self, payload):
        if not self.client_id or not self.access_token:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        try:
            r = self.session.post(f"{API}/marketfeed/ohlc", json=payload, timeout=(5, 25))
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Dhan request failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Dhan returned invalid JSON.") from exc
        if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data))
        return data


@lru_cache(maxsize=1)
def universe():
    r = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20))
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content))
    cols = {norm(c): c for c in df.columns}
    col = cols.get("SYMBOL") or cols.get("SYMBOLNAME")
    if not col:
        raise RuntimeError(f"NIFTY 500 list has no symbol column: {list(df.columns)[:20]}")
    return sorted({str(x).strip().upper() for x in df[col].dropna() if str(x).strip()})


@lru_cache(maxsize=1)
def master():
    r = requests.get(DHAN_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 30))
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), low_memory=False, on_bad_lines="skip")
    cols = {norm(c): c for c in df.columns}

    def find(*names):
        for name in names:
            if norm(name) in cols:
                return cols[norm(name)]
        for name in names:
            n = norm(name)
            for k, original in cols.items():
                if n in k or k in n:
                    return original
        return None

    sid = find("SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID", "SECURITY_ID", "SECURITYID")
    symbol = find("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "TRADING_SYMBOL", "TRADINGSYMBOL", "SYMBOLNAME")
    segment = find("SEM_SEGMENT", "SEGMENT", "SEM_EXM_EXCH_ID", "EXCHANGE_ID", "EXCHID", "EXCHANGE")
    if not sid or not symbol:
        raise RuntimeError(f"Dhan master schema not recognized: {list(df.columns)[:30]}")

    equities, indices = {}, []
    for _, row in df.iterrows():
        s = str(row.get(symbol, "")).strip().upper()
        i = str(row.get(sid, "")).strip()
        seg = str(row.get(segment, "")).upper() if segment else ""
        if not s or s in {"NAN", "NONE"} or not i.isdigit():
            continue
        item = {"symbol": s, "security_id": i}
        if "IDX" in seg or "INDEX" in seg or "NIFTY 500" in s or "NIFTY500" in s:
            indices.append(item)
        else:
            equities.setdefault(s, item)
    return equities, indices


class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(float(risk_per_trade), float(max_daily_loss), float(target_rr))
        self.state_file = Path("orb_state.json")
        self.last_error = ""
        self._today = date.today()
        self._cache = {"at": 0.0, "df": None, "index": None}
        self.state = self._load_state()

    def _load_state(self):
        try:
            state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {"positions": []}
        except Exception:
            state = {"positions": []}
        if not isinstance(state, dict): state = {"positions": []}
        state.setdefault("positions", [])
        return state

    def daily_pnl(self):
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == str(self._today)))

    @staticmethod
    def _root(data, key):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        return root.get(key, {}) if isinstance(root, dict) else {}

    def stock_scan(self):
        now = time.monotonic()
        if self._cache["df"] is not None and now - self._cache["at"] < 15:
            return self._cache["df"]
        try:
            names = universe()
            equities, indices = master()
            configs = [equities[s] for s in names if s in equities]
            if not configs: raise RuntimeError("No NIFTY 500 NSE equity instruments matched Dhan master.")
            index_candidates = [x for x in indices if norm(x["symbol"]) in {"NIFTY500", "NIFTY500INDEX"} or "NIFTY500" in norm(x["symbol"])]
            payload = {"NSE_EQ": [int(x["security_id"]) for x in configs]}
            if index_candidates: payload["NSE_IDX"] = [int(x["security_id"]) for x in index_candidates[:3]]
            data = self.dhan.ohlc(payload)
            eq = self._root(data, "NSE_EQ")
            idx = self._root(data, "NSE_IDX")
            self._cache["index"] = None
            for item in index_candidates:
                q = idx.get(item["security_id"]) or idx.get(int(item["security_id"]))
                if isinstance(q, dict):
                    o = q.get("ohlc") or {}
                    ltp = q.get("last_price")
                    if ltp not in (None, ""):
                        pdc = o.get("close")
                        self._cache["index"] = {"LTP": float(ltp), "PDC": float(pdc) if pdc not in (None, "", 0) else None}
                        break
            rows = []
            for c in configs:
                q = eq.get(c["security_id"]) or eq.get(int(c["security_id"]))
                if not isinstance(q, dict) or q.get("last_price") in (None, ""): continue
                o = q.get("ohlc") or {}; ltp = float(q["last_price"]); pdc = o.get("close")
                pdcv = float(pdc) if pdc not in (None, "", 0) else None
                rows.append({"Symbol": c["symbol"], "LTP": ltp, "Open": o.get("open"), "PDC": pdc, "Today %": ((ltp-pdcv)/pdcv*100) if pdcv else None, "1W %": None, "1M %": None, "3M %": None, "ORB High": None, "ORB Low": None, "Buy condition": "WAIT"})
            result = pd.DataFrame(rows)
            self.last_error = "" if not result.empty else "Dhan returned no NIFTY 500 quotes."
        except Exception as exc:
            self.last_error = f"NIFTY 500 scan error: {type(exc).__name__}: {exc}"
            result = pd.DataFrame([{"Status": self.last_error}])
        self._cache = {"at": now, "df": result, "index": self._cache.get("index")}
        return result

    def index_metrics(self):
        self.stock_scan()
        return self._cache.get("index")

    def setup_table(self, direction):
        df = self.stock_scan()
        if "Buy condition" not in df.columns: return df
        return df[df["Buy condition"] == "BUY"].reset_index(drop=True) if direction == "BUY" else df[df["Buy condition"] != "BUY"].reset_index(drop=True)

    def today_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])
    def past_positions(self): return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])
    def strategy_markdown(self): return "**Universe:** NSE NIFTY 500. **Live data:** one Dhan batch containing NSE equities and the NIFTY 500 index per 15-second refresh. Paper trading only."
    def config_table(self): return pd.DataFrame()
