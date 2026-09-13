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
RATE_LIMIT_COOLDOWN = 60.0


def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def clean(value):
    text = str(value or "").strip().upper()
    return "" if text in {"", "NAN", "NONE", "NULL"} else text


@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0


class DhanClient:
    def __init__(self, client_id, access_token):
        self.client_id = clean(client_id)
        self.access_token = str(access_token or "").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.access_token,
            "client-id": self.client_id,
        })
        self._rate_limit_until = 0.0

    def ohlc(self, payload):
        if not self.client_id or not self.access_token:
            raise RuntimeError("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in Streamlit secrets.")
        now = time.monotonic()
        if now < self._rate_limit_until:
            remaining = max(1, int(self._rate_limit_until - now))
            raise RuntimeError(f"Dhan rate limit cooldown active ({remaining}s). No retry storm.")

        response = self.session.post(
            f"{API}/marketfeed/ohlc",
            json=payload,
            timeout=(5, 25),
        )
        if response.status_code == 429:
            self._rate_limit_until = time.monotonic() + RATE_LIMIT_COOLDOWN
            raise RuntimeError("Dhan rate limit 429. Cooling down for 60 seconds; no repeated retries.")
        if response.status_code >= 400:
            raise RuntimeError(
                f"Dhan HTTP {response.status_code}: {response.text[:400].replace(chr(10), ' ')}"
            )
        try:
            data = response.json()
        except Exception:
            raise RuntimeError(f"Dhan returned non-JSON response: {response.text[:300]}")
        if isinstance(data, dict) and str(data.get("status", "")).lower() in {"failure", "failed", "error"}:
            raise RuntimeError(str(data.get("remarks") or data.get("message") or data))
        self._rate_limit_until = 0.0
        return data


@lru_cache(maxsize=1)
def universe():
    response = requests.get(NIFTY500_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=(5, 20))
    response.raise_for_status()
    frame = pd.read_csv(io.BytesIO(response.content))
    columns = {norm(c): c for c in frame.columns}
    symbol_col = columns.get("SYMBOL") or columns.get("SYMBOLNAME")
    if not symbol_col:
        raise RuntimeError(f"NIFTY 500 CSV has no symbol column: {list(frame.columns)[:20]}")
    return sorted({clean(v) for v in frame[symbol_col].dropna() if clean(v)})


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
    if not security_col or not (trading_col or custom_col):
        raise RuntimeError(f"Dhan master schema not recognized. Columns: {list(frame.columns)[:30]}")

    equities, indices = {}, []
    for _, row in frame.iterrows():
        trading = clean(row.get(trading_col, "")) if trading_col else ""
        custom = clean(row.get(custom_col, "")) if custom_col else ""
        symbol = trading or custom
        security_id = clean(row.get(security_col, ""))
        segment = clean(row.get(segment_col, "")) if segment_col else ""
        exchange = clean(row.get(exchange_col, "")) if exchange_col else ""
        instrument = clean(row.get(instrument_col, "")) if instrument_col else ""
        if not symbol or not security_id.isdigit():
            continue
        item = {
            "symbol": symbol,
            "custom": custom,
            "trading": trading,
            "security_id": security_id,
            "segment": segment,
            "exchange": exchange,
            "instrument": instrument,
        }
        match_text = norm(f"{symbol} {trading} {custom} {segment} {exchange} {instrument}")
        is_index = (
            "INDEX" in instrument
            or "INDEX" in segment
            or "IDX" in segment
            or "IDX" in exchange
            or norm(symbol) == "NIFTY500"
            or norm(custom) == "NIFTY500"
            or "NIFTY500" in match_text
        )
        if is_index:
            indices.append(item)
        elif exchange in {"NSE", "NSE_EQ", "NSECM"} or not exchange_col:
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

    @staticmethod
    def _nifty500_score(item):
        symbol = norm(item.get("symbol"))
        custom = norm(item.get("custom"))
        trading = norm(item.get("trading"))
        instrument = norm(item.get("instrument"))
        segment = norm(item.get("segment"))
        exchange = norm(item.get("exchange"))
        score = 0
        if symbol == "NIFTY500": score += 1000
        if custom == "NIFTY500": score += 900
        if trading == "NIFTY500": score += 800
        if "NIFTY500" in symbol: score += 500
        if "NIFTY500" in custom: score += 400
        if "NIFTY500" in trading: score += 300
        if instrument == "INDEX": score += 100
        if segment in {"I", "IDXI", "IDX_I"}: score += 50
        if exchange == "NSE": score += 25
        return score

    def _nifty500_candidates(self, indices):
        matches = []
        seen = set()
        for item in indices:
            text = norm(f"{item.get('symbol','')} {item.get('custom','')} {item.get('trading','')} {item.get('instrument','')}")
            if "NIFTY500" not in text:
                continue
            sid = str(item.get("security_id"))
            if not sid or sid in seen:
                continue
            seen.add(sid)
            matches.append(item)
        return sorted(matches, key=self._nifty500_score, reverse=True)

    def stock_scan(self):
        now = time.monotonic()
        if self._cache["df"] is not None and now - self._cache["at"] < LIVE_TTL:
            return self._cache["df"]
        try:
            names = universe()
            equities, indices = master()
            configs = [equities[s] for s in names if s in equities]
            if not configs:
                raise RuntimeError("No NIFTY 500 equities matched the Dhan instrument master.")

            index_candidates = self._nifty500_candidates(indices)
            payload = {"NSE_EQ": [int(item["security_id"]) for item in configs]}
            if index_candidates:
                # Try several exact matches in one request; Dhan supports batch market-feed requests.
                payload["IDX_I"] = [int(item["security_id"]) for item in index_candidates[:10]]

            data = self.dhan.ohlc(payload)
            eq = self._root(data, "NSE_EQ")
            idx = self._root(data, "IDX_I", "NSE_IDX", "IDX", "NSE_INDEX")

            index_value = None
            chosen_index = None
            for item in index_candidates:
                quote = self._quote(idx, item["security_id"])
                if not isinstance(quote, dict):
                    continue
                ohlc = quote.get("ohlc") or {}
                live_ltp = self._number(quote.get("last_price") or quote.get("ltp"))
                close = self._number(ohlc.get("close") or quote.get("previous_close") or quote.get("prev_close"))
                display_ltp = live_ltp or close
                if display_ltp is None:
                    continue
                index_value = {
                    "LTP": display_ltp,
                    "PDC": close,
                    "Today %": ((live_ltp - close) / close * 100) if live_ltp is not None and close else 0.0 if close else None,
                    "Security ID": str(item["security_id"]),
                    "Live": live_ltp is not None,
                    "Market status": "LIVE" if live_ltp is not None else "CLOSED / LAST CLOSE",
                }
                chosen_index = item
                break

            rows = []
            for item in configs:
                quote = self._quote(eq, item["security_id"])
                if not isinstance(quote, dict):
                    continue
                ltp = self._number(quote.get("last_price") or quote.get("ltp"))
                ohlc = quote.get("ohlc") or {}
                pdc = self._number(ohlc.get("close"))
                if ltp is None and pdc is not None:
                    ltp = pdc
                if ltp is None:
                    continue
                rows.append({
                    "Symbol": item["symbol"],
                    "LTP": ltp,
                    "Open": ohlc.get("open"),
                    "PDC": pdc,
                    "Today %": ((ltp - pdc) / pdc * 100) if pdc else None,
                    "1W %": None,
                    "1M %": None,
                    "3M %": None,
                    "ORB High": None,
                    "ORB Low": None,
                    "Buy condition": "WAIT",
                })
            if not rows:
                raise RuntimeError("Dhan returned no NSE_EQ quotes for the NIFTY 500 batch.")

            if index_value:
                self.last_error = ""
            else:
                ids = [item["security_id"] for item in index_candidates]
                segments = list((data.get("data") or {}).keys()) if isinstance(data, dict) else "unknown"
                self.last_error = f"Stock quotes work, but exact NIFTY 500 index quote was not returned. Tried IDs: {ids}. Response segments: {segments}."

            result = pd.DataFrame(rows)
            self._cache = {"at": now, "df": result, "index": index_value}
            return result
        except Exception as exc:
            self.last_error = f"Live data error: {type(exc).__name__}: {exc}"
            # Keep last successful scan/index rather than replacing it with an error table.
            if self._cache.get("df") is not None:
                return self._cache["df"]
            return pd.DataFrame([{"Status": self.last_error}])

    def index_metrics(self):
        self.stock_scan()
        return self._cache.get("index")

    def setup_table(self, direction):
        frame = self.stock_scan()
        if "Buy condition" not in frame.columns:
            return frame
        wanted = "BUY" if direction == "BUY" else "SELL"
        return frame[frame["Buy condition"] == wanted].reset_index(drop=True)

    def today_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])

    def past_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])

    def strategy_markdown(self):
        return "**Universe:** NSE NIFTY 500. **Live data:** Dhan market data with 15-second dashboard refresh. **Execution:** paper trading only."

    def config_table(self):
        try:
            _, indices = master()
            matches = self._nifty500_candidates(indices)
            return pd.DataFrame([
                {
                    "Type": "NIFTY 500 index",
                    "Symbol": x.get("symbol"),
                    "Custom symbol": x.get("custom"),
                    "Trading symbol": x.get("trading"),
                    "Segment": x.get("segment"),
                    "Exchange": x.get("exchange"),
                    "Instrument": x.get("instrument"),
                    "Security ID": x.get("security_id"),
                    "Score": self._nifty500_score(x),
                }
                for x in matches
            ])
        except Exception as exc:
            return pd.DataFrame([{"Status": str(exc)}])
