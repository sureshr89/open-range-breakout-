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
LIVE_TTL = 15.0


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
        last_error = ""
        for wait_seconds in (0, 3, 8):
            if wait_seconds:
                time.sleep(wait_seconds)
            response = self.session.post(f"{API}/marketfeed/ohlc", json=payload, timeout=(5, 25))
            if response.status_code == 429:
                last_error = "Dhan rate limit 429"
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Dhan HTTP {response.status_code}: {response.text[:300].replace(chr(10), ' ')}")
            data = response.json()
            if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
                raise RuntimeError(str(data.get("remarks") or data.get("message") or data))
            return data
        raise RuntimeError(f"{last_error}. Retry on the next refresh.")


@lru_cache(maxsize=1)
def universe():
    response = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20))
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content))
    columns = {norm(c): c for c in frame.columns}
    symbol_col = columns.get("SYMBOL") or columns.get("SYMBOLNAME")
    if not symbol_col:
        raise RuntimeError(f"NIFTY 500 CSV has no symbol column: {list(frame.columns)[:20]}")
    return sorted({str(v).strip().upper() for v in frame[symbol_col].dropna() if str(v).strip()})


@lru_cache(maxsize=1)
def master():
    response = requests.get(DHAN_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 30))
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content), low_memory=False, on_bad_lines="skip")
    columns = {norm(c): c for c in frame.columns}

    def find(*names):
        for name in names:
            if norm(name) in columns:
                return columns[norm(name)]
        return None

    security_col = find("SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID", "SECURITY_ID", "SECURITYID")
    trading_col = find("SEM_TRADING_SYMBOL", "TRADING_SYMBOL", "TRADINGSYMBOL")
    custom_col = find("SEM_CUSTOM_SYMBOL", "CUSTOM_SYMBOL", "SYMBOLNAME")
    segment_col = find("SEM_SEGMENT", "SEGMENT")
    exchange_col = find("SEM_EXM_EXCH_ID", "EXCHANGE_ID", "EXCHID", "EXCHANGE")
    instrument_col = find("SEM_INSTRUMENT_NAME", "INSTRUMENT_NAME", "INSTRUMENT")
    if not security_col or not trading_col and not custom_col:
        raise RuntimeError(f"Dhan master schema not recognized: {list(frame.columns)[:30]}")

    equities, indices = {}, []
    for _, row in frame.iterrows():
        trading = str(row.get(trading_col, "")).strip().upper() if trading_col else ""
        custom = str(row.get(custom_col, "")).strip().upper() if custom_col else ""
        symbol = trading if trading and trading not in {"NAN", "NONE"} else custom
        security_id = str(row.get(security_col, "")).strip()
        segment = str(row.get(segment_col, "")).upper() if segment_col else ""
        exchange = str(row.get(exchange_col, "")).upper() if exchange_col else ""
        instrument = str(row.get(instrument_col, "")).upper() if instrument_col else ""
        match_text = norm(f"{trading} {custom} {segment} {exchange} {instrument}")
        if not symbol or not security_id.isdigit():
            continue
        item = {"symbol": symbol, "security_id": security_id}
        is_index = "IDX" in segment or "INDEX" in segment or "IDX" in exchange or "INDEX" in instrument or "NIFTY500" in match_text
        if is_index:
            indices.append(item)
        elif exchange in {"NSE", "NSE_EQ"} or not exchange_col:
            equities.setdefault(symbol, item)
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
            value = json.loads(self.state_file.read_text()) if self.state_file.exists() else {"positions": []}
        except Exception:
            value = {"positions": []}
        if not isinstance(value, dict):
            value = {"positions": []}
        value.setdefault("positions", [])
        return value

    def daily_pnl(self):
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == str(self._today)))

    @staticmethod
    def _root(data, *keys):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        if not isinstance(root, dict):
            return {}
        for key in keys:
            value = root.get(key)
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _quote(bucket, security_id):
        if not isinstance(bucket, dict):
            return None
        return bucket.get(str(security_id)) or bucket.get(int(security_id))

    @staticmethod
    def _number(value):
        try:
            number = float(value)
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    def stock_scan(self):
        now = time.monotonic()
        if self._cache["df"] is not None and now - self._cache["at"] < LIVE_TTL:
            return self._cache["df"]
        try:
            names = universe()
            equities, indices = master()
            configs = [equities[s] for s in names if s in equities]
            if not configs:
                raise RuntimeError("No NSE NIFTY 500 equities matched the Dhan instrument master.")
            index_candidates = [i for i in indices if "NIFTY500" in norm(i["symbol"])]
            payload = {"NSE_EQ": [int(i["security_id"]) for i in configs]}
            if index_candidates:
                payload["IDX_I"] = [int(i["security_id"]) for i in index_candidates[:1]]
            data = self.dhan.ohlc(payload)
            eq = self._root(data, "NSE_EQ")
            idx = self._root(data, "IDX_I", "NSE_IDX", "IDX")
            index_value = None
            for item in index_candidates:
                quote = self._quote(idx, item["security_id"])
                if isinstance(quote, dict):
                    ltp = self._number(quote.get("last_price"))
                    close = self._number((quote.get("ohlc") or {}).get("close"))
                    if ltp is not None:
                        index_value = {"LTP": ltp, "PDC": close, "Today %": ((ltp - close) / close * 100) if close else None}
                        break
            rows = []
            for item in configs:
                quote = self._quote(eq, item["security_id"])
                if not isinstance(quote, dict):
                    continue
                ltp = self._number(quote.get("last_price"))
                if ltp is None:
                    continue
                ohlc = quote.get("ohlc") or {}
                pdc = self._number(ohlc.get("close"))
                rows.append({"Symbol": item["symbol"], "LTP": ltp, "Open": ohlc.get("open"), "PDC": pdc, "Today %": ((ltp - pdc) / pdc * 100) if pdc else None, "1W %": None, "1M %": None, "3M %": None, "ORB High": None, "ORB Low": None, "Buy condition": "WAIT"})
            if not rows:
                raise RuntimeError("Dhan returned no NSE_EQ quotes for the NIFTY 500 batch.")
            self.last_error = "" if index_value else f"NIFTY 500 index quote not found; matched index instruments: {len(index_candidates)}. Stock data is available."
            result = pd.DataFrame(rows)
            self._cache = {"at": now, "df": result, "index": index_value}
            return result
        except Exception as exc:
            self.last_error = f"NIFTY 500 scan error: {type(exc).__name__}: {exc}"
            result = pd.DataFrame([{"Status": self.last_error}])
            self._cache = {"at": now, "df": result, "index": None}
            return result

    def index_metrics(self):
        self.stock_scan()
        return self._cache.get("index")

    def setup_table(self, direction):
        frame = self.stock_scan()
        if "Buy condition" not in frame.columns:
            return frame
        if direction == "BUY":
            return frame[frame["Buy condition"] == "BUY"].reset_index(drop=True)
        return frame[frame["Buy condition"] == "SELL"].reset_index(drop=True)

    def today_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])

    def past_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])

    def strategy_markdown(self):
        return "**Universe:** NSE NIFTY 500. **Live data:** Dhan batch refresh every 15 seconds. Paper trading only."

    def config_table(self):
        return pd.DataFrame()
