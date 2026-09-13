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
LIVE_CACHE_FILE = Path(".dhan_live_cache.json")


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
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        })

    def ohlc(self, payload):
        if not self.client_id or not self.access_token:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        try:
            response = self.session.post(
                f"{API}/marketfeed/ohlc",
                json=payload,
                timeout=(5, 25),
            )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "15")
                raise RuntimeError(
                    f"Dhan rate limit (429). Keep one request per 15 seconds; retry after {retry_after}s."
                )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Dhan request failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Dhan returned invalid JSON.") from exc
        if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data))
        return data


@lru_cache(maxsize=1)
def universe():
    response = requests.get(
        NIFTY500_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content))
    columns = {norm(c): c for c in frame.columns}
    symbol_col = columns.get("SYMBOL") or columns.get("SYMBOLNAME")
    if not symbol_col:
        raise RuntimeError(f"NIFTY 500 list has no symbol column: {list(frame.columns)[:20]}")
    return sorted({
        str(value).strip().upper()
        for value in frame[symbol_col].dropna()
        if str(value).strip()
    })


@lru_cache(maxsize=1)
def master():
    response = requests.get(
        DHAN_MASTER_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=(5, 30),
    )
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content), low_memory=False, on_bad_lines="skip")
    columns = {norm(c): c for c in frame.columns}

    def find(*names):
        for name in names:
            if norm(name) in columns:
                return columns[norm(name)]
        for name in names:
            wanted = norm(name)
            for normalized, original in columns.items():
                if wanted in normalized or normalized in wanted:
                    return original
        return None

    security_col = find(
        "SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID", "SECURITY_ID", "SECURITYID"
    )
    symbol_col = find(
        "SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "TRADING_SYMBOL",
        "TRADINGSYMBOL", "SYMBOLNAME"
    )
    segment_col = find(
        "SEM_SEGMENT", "SEGMENT", "SEM_EXM_EXCH_ID", "EXCHANGE_ID",
        "EXCHID", "EXCHANGE"
    )
    if not security_col or not symbol_col:
        raise RuntimeError(f"Dhan master schema not recognized: {list(frame.columns)[:30]}")

    equities, indices = {}, []
    for _, row in frame.iterrows():
        symbol = str(row.get(symbol_col, "")).strip().upper()
        security_id = str(row.get(security_col, "")).strip()
        segment = str(row.get(segment_col, "")).upper() if segment_col else ""
        if not symbol or symbol in {"NAN", "NONE"} or not security_id.isdigit():
            continue
        item = {"symbol": symbol, "security_id": security_id}
        if "IDX" in segment or "INDEX" in segment or "NIFTY500" in norm(symbol):
            indices.append(item)
        else:
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
    def _root(data, key):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        return root.get(key, {}) if isinstance(root, dict) else {}

    def _read_disk_cache(self):
        try:
            if not LIVE_CACHE_FILE.exists():
                return None
            payload = json.loads(LIVE_CACHE_FILE.read_text())
            if time.time() - float(payload.get("at", 0)) < 15 and isinstance(payload.get("rows"), list):
                return payload
        except Exception:
            return None
        return None

    def _write_disk_cache(self, rows, index):
        try:
            LIVE_CACHE_FILE.write_text(json.dumps({"at": time.time(), "rows": rows, "index": index}))
        except Exception:
            pass

    def stock_scan(self):
        now = time.monotonic()
        if self._cache["df"] is not None and now - self._cache["at"] < 15:
            return self._cache["df"]

        disk = self._read_disk_cache()
        if disk:
            result = pd.DataFrame(disk["rows"])
            self._cache = {"at": now, "df": result, "index": disk.get("index")}
            self.last_error = ""
            return result

        try:
            names = universe()
            equities, indices = master()
            configs = [equities[s] for s in names if s in equities]
            if not configs:
                raise RuntimeError("No NIFTY 500 NSE equity instruments matched Dhan master.")

            index_candidates = [
                item for item in indices
                if norm(item["symbol"]) in {"NIFTY500", "NIFTY500INDEX"}
                or "NIFTY500" in norm(item["symbol"])
            ]
            payload = {"NSE_EQ": [int(item["security_id"]) for item in configs]}
            if index_candidates:
                payload["NSE_IDX"] = [int(item["security_id"]) for item in index_candidates[:3]]

            data = self.dhan.ohlc(payload)
            eq = self._root(data, "NSE_EQ")
            idx = self._root(data, "NSE_IDX")
            index_value = None
            for item in index_candidates:
                quote = idx.get(item["security_id"]) or idx.get(int(item["security_id"]))
                if isinstance(quote, dict) and quote.get("last_price") not in (None, ""):
                    ohlc = quote.get("ohlc") or {}
                    pdc = ohlc.get("close")
                    index_value = {
                        "LTP": float(quote["last_price"]),
                        "PDC": float(pdc) if pdc not in (None, "", 0) else None,
                    }
                    break

            rows = []
            for item in configs:
                quote = eq.get(item["security_id"]) or eq.get(int(item["security_id"]))
                if not isinstance(quote, dict) or quote.get("last_price") in (None, ""):
                    continue
                ohlc = quote.get("ohlc") or {}
                ltp = float(quote["last_price"])
                pdc = ohlc.get("close")
                pdc_value = float(pdc) if pdc not in (None, "", 0) else None
                rows.append({
                    "Symbol": item["symbol"],
                    "LTP": ltp,
                    "Open": ohlc.get("open"),
                    "PDC": pdc,
                    "Today %": ((ltp - pdc_value) / pdc_value * 100) if pdc_value else None,
                    "1W %": None,
                    "1M %": None,
                    "3M %": None,
                    "ORB High": None,
                    "ORB Low": None,
                    "Buy condition": "WAIT",
                })

            result = pd.DataFrame(rows)
            self.last_error = "" if not result.empty else "Dhan returned no NIFTY 500 quotes."
            self._write_disk_cache(rows, index_value)
            self._cache = {"at": now, "df": result, "index": index_value}
            return result
        except Exception as exc:
            self.last_error = f"NIFTY 500 scan error: {type(exc).__name__}: {exc}"
            stale = self._read_disk_cache()
            if stale and isinstance(stale.get("rows"), list):
                result = pd.DataFrame(stale["rows"])
                self._cache = {"at": now, "df": result, "index": stale.get("index")}
                return result
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
        return frame[frame["Buy condition"] != "BUY"].reset_index(drop=True)

    def today_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])

    def past_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])

    def strategy_markdown(self):
        return "**Universe:** NSE NIFTY 500. **Live data:** one Dhan batch containing NSE equities and the NIFTY 500 index per 15-second refresh. Paper trading only."

    def config_table(self):
        return pd.DataFrame()
