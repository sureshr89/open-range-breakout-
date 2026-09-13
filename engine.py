from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
import io
import json
import re
import time

import pandas as pd
import requests

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
        self.client_id = str(client_id or "").strip()
        self.access_token = str(access_token or "").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        })

    @property
    def ready(self):
        return bool(self.client_id and self.access_token)

    def post(self, path: str, payload: dict):
        if not self.ready:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        try:
            response = self.session.post(f"{API}{path}", json=payload, timeout=(5, 20))
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Dhan request failed: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Dhan returned an invalid JSON response.") from exc
        if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data))
        return data

    def ohlc(self, securities: dict):
        return self.post("/marketfeed/ohlc", securities)


def _normal(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


@lru_cache(maxsize=1)
def _cached_universe():
    response = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20))
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content))
    columns = {_normal(column): column for column in frame.columns}
    symbol_column = columns.get("SYMBOL") or columns.get("SYMBOLNAME")
    if symbol_column is None:
        raise RuntimeError(f"NIFTY 500 CSV has no symbol column: {list(frame.columns)[:20]}")
    return sorted({str(value).strip().upper() for value in frame[symbol_column].dropna() if str(value).strip()})


@lru_cache(maxsize=1)
def _cached_master():
    response = requests.get(DHAN_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 30))
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content), low_memory=False, on_bad_lines="skip")
    normalized = {_normal(column): column for column in frame.columns}

    def find_column(*candidates):
        for candidate in candidates:
            key = _normal(candidate)
            if key in normalized:
                return normalized[key]
        for candidate in candidates:
            key = _normal(candidate)
            for actual, original in normalized.items():
                if key in actual or actual in key:
                    return original
        return None

    security_column = find_column(
        "SEM_SMST_SECURITY_ID", "SEM_SECURITY_ID", "SECURITY_ID", "SECURITYID"
    )
    symbol_column = find_column(
        "SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "TRADING_SYMBOL", "TRADINGSYMBOL", "SYMBOLNAME"
    )
    exchange_column = find_column(
        "SEM_EXM_EXCH_ID", "SEM_EXCH_ID", "EXCHANGE_ID", "EXCHID", "EXCHANGE"
    )
    if security_column is None or symbol_column is None:
        raise RuntimeError(f"Dhan master schema not recognized: {list(frame.columns)[:30]}")

    if exchange_column is not None:
        allowed = {"NSE", "NSE_EQ", "NSECM", "NSE CM"}
        frame = frame[frame[exchange_column].astype(str).str.upper().str.strip().isin(allowed)]

    result = {}
    for _, row in frame.iterrows():
        symbol = str(row.get(symbol_column, "")).strip().upper()
        security_id = str(row.get(security_column, "")).strip()
        if symbol in {"", "NAN", "NONE"} or not security_id.isdigit():
            continue
        result.setdefault(symbol, {"symbol": symbol, "security_id": security_id})
    return result


class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(float(risk_per_trade), float(max_daily_loss), float(target_rr))
        self.cache_file = Path("orb_state.json")
        self.last_error = ""
        self._today = date.today()
        self._scan_cache = {"at": 0.0, "df": None}
        self.state = self._load_state()

    def _load_state(self):
        try:
            state = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {"positions": []}
        except (OSError, ValueError, TypeError):
            state = {"positions": []}
        if not isinstance(state, dict):
            state = {"positions": []}
        state.setdefault("positions", [])
        return state

    def daily_pnl(self):
        return float(sum(float(position.get("pnl", 0)) for position in self.state["positions"] if str(position.get("date")) == str(self._today)))

    def _load_universe(self):
        return _cached_universe()

    def _load_master(self):
        return _cached_master()

    @staticmethod
    def _quote_map(data):
        root = data.get("data", {}) if isinstance(data, dict) else {}
        return root.get("NSE_EQ", {}) if isinstance(root, dict) else {}

    def stock_scan(self):
        now = time.monotonic()
        if self._scan_cache["df"] is not None and now - self._scan_cache["at"] < 15:
            return self._scan_cache["df"]
        try:
            universe = self._load_universe()
            master = self._load_master()
            configs = [master[symbol] for symbol in universe if symbol in master]
            if not configs:
                raise RuntimeError("No NIFTY 500 NSE equity instruments matched the Dhan master.")
            payload = {"NSE_EQ": [int(item["security_id"]) for item in configs]}
            quotes = self.dhan.ohlc(payload)
            quote_map = self._quote_map(quotes)
            rows = []
            for config in configs:
                item = quote_map.get(str(config["security_id"])) or quote_map.get(int(config["security_id"]))
                if not isinstance(item, dict):
                    continue
                ohlc = item.get("ohlc") or {}
                ltp = item.get("last_price")
                if ltp in (None, ""):
                    continue
                ltp_value = float(ltp)
                pdc = ohlc.get("close")
                pdc_value = float(pdc) if pdc not in (None, "", 0) else None
                rows.append({
                    "Symbol": config["symbol"],
                    "LTP": ltp_value,
                    "Open": ohlc.get("open"),
                    "PDC": pdc,
                    "Today %": ((ltp_value - pdc_value) / pdc_value * 100) if pdc_value else None,
                    "1W %": None,
                    "1M %": None,
                    "3M %": None,
                    "ORB High": None,
                    "ORB Low": None,
                    "Buy condition": "WAIT",
                })
            result = pd.DataFrame(rows)
            self.last_error = "" if not result.empty else "Dhan returned no NIFTY 500 quotes."
        except Exception as exc:
            self.last_error = f"NIFTY 500 scan error: {type(exc).__name__}: {exc}"
            result = pd.DataFrame([{"Status": self.last_error}])
        self._scan_cache = {"at": now, "df": result}
        return result

    def setup_table(self, direction):
        frame = self.stock_scan()
        if frame.empty or "Buy condition" not in frame.columns:
            return frame
        if direction == "BUY":
            return frame[frame["Buy condition"] == "BUY"].reset_index(drop=True)
        return frame[frame["Buy condition"] != "BUY"].reset_index(drop=True)

    def today_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])

    def past_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])

    def strategy_markdown(self):
        return "**Universe:** NSE NIFTY 500. **Live data:** one Dhan NSE_EQ OHLC batch per 15-second refresh. **ORB/history:** cached values only when available. Paper trading only."

    def config_table(self):
        return pd.DataFrame()
