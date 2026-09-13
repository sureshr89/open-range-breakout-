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
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json", "access-token": self.access_token, "client-id": self.client_id})

    @property
    def ready(self):
        return bool(self.client_id and self.access_token)

    def _post(self, path, payload):
        if not self.ready:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        response = self.session.post(f"{API}{path}", json=payload, timeout=30)
        try:
            data = response.json()
        except Exception:
            response.raise_for_status()
            raise RuntimeError(f"Dhan returned HTTP {response.status_code}")
        if response.status_code >= 400 or (isinstance(data, dict) and str(data.get("status", "")).lower() == "failure"):
            message = data.get("remarks") or data.get("message") or data.get("errorMessage") or str(data)
            raise RuntimeError(f"Dhan HTTP {response.status_code}: {message}")
        return data

    def ltp(self, securities):
        return self._post("/marketfeed/ltp", securities)

    def ohlc(self, securities):
        return self._post("/marketfeed/ohlc", securities)

    def daily(self, security_id, exchange, instrument, from_date, to_date):
        return self._post("/charts/historical", {"securityId": str(security_id), "exchangeSegment": exchange, "instrument": instrument, "expiryCode": 0, "oi": False, "fromDate": from_date, "toDate": to_date})

    def intraday(self, security_id, exchange, instrument, interval, from_dt, to_dt):
        return self._post("/charts/intraday", {"securityId": str(security_id), "exchangeSegment": exchange, "instrument": instrument, "interval": interval, "oi": False, "fromDate": from_dt, "toDate": to_dt})

def _to_df(data):
    if not isinstance(data, dict) or "timestamp" not in data:
        return pd.DataFrame()
    n = len(data.get("timestamp", []))
    frame = pd.DataFrame({k: data.get(k, [None] * n) for k in ["timestamp", "open", "high", "low", "close", "volume"]})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s", errors="coerce")
    return frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

class ORBEngine:
    def __init__(self, client_id, access_token, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(risk_per_trade, max_daily_loss, target_rr)
        self.cache_file = Path("orb_state.json")
        self.config = dict(DEFAULT_CONFIG)
        self.last_error = ""
        self._today = date.today()
        self._load_state()
        self._universe_cache = None
        self._master_cache = None
        self._stock_cache = {}
        self._scan_cache = {"at": 0.0, "df": None}

    def _load_state(self):
        try:
            self.state = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else {"positions": []}
        except Exception:
            self.state = {"positions": []}
        self.state.setdefault("positions", [])

    def daily_pnl(self):
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == str(self._today)))

    def _cfg(self):
        return self.config["NIFTY"]

    def _quote_item(self, data, segment, sid):
        block = data.get("data", {}).get(segment, {}) if isinstance(data, dict) else {}
        return block.get(str(sid)) or block.get(int(sid))

    def _nifty500_ltp(self):
        c = self._cfg()
        item = self._quote_item(self.dhan.ltp({c["exchange"]: [int(c["security_id"])]}), c["exchange"], c["security_id"])
        if not item:
            raise RuntimeError("Dhan returned no NIFTY quote for securityId 13")
        return float(item["last_price"])

    def reference_levels(self, c):
        key = c["symbol"]
        cached = self._stock_cache.get(key)
        if cached and time_module.monotonic() - cached[0] < 900:
            return cached[1]
        frame = _to_df(self.dhan.daily(c["security_id"], c["exchange"], c["instrument"], (self._today - timedelta(days=150)).strftime("%Y-%m-%d"), (self._today + timedelta(days=1)).strftime("%Y-%m-%d")))
        closes = pd.to_numeric(frame.get("close", pd.Series(dtype=float)), errors="coerce").dropna().reset_index(drop=True)
        refs = {"pdc": None, "week_close": None, "month_close": None, "quarter_close": None}
        if len(closes) >= 2:
            refs["pdc"] = float(closes.iloc[-2])
        if len(closes) >= 6:
            refs["week_close"] = float(closes.iloc[-6])
        if len(closes) >= 22:
            refs["month_close"] = float(closes.iloc[-22])
        if len(closes) >= 66:
            refs["quarter_close"] = float(closes.iloc[-66])
        self._stock_cache[key] = (time_module.monotonic(), refs)
        return refs

    def opening_range(self, c):
        frame = _to_df(self.dhan.intraday(c["security_id"], c["exchange"], c["instrument"], "1", f"{self._today} 09:15:00", f"{self._today} 09:30:00"))
        if frame.empty:
            return None
        frame = frame[(frame.timestamp.dt.time >= time(9, 15)) & (frame.timestamp.dt.time < time(9, 30))]
        return None if frame.empty else {"high": float(frame.high.max()), "low": float(frame.low.min())}

    def _load_universe(self):
        if self._universe_cache is not None:
            return self._universe_cache
        response = requests.get(NIFTY500_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        frame = pd.read_csv(io.BytesIO(response.content))
        columns = {str(c).strip().lower(): c for c in frame.columns}
        symbol_col = columns.get("symbol") or columns.get("symbol name")
        if not symbol_col:
            raise RuntimeError(f"NIFTY 500 CSV has no Symbol column. Columns: {list(frame.columns)}")
        self._universe_cache = sorted({str(x).strip().upper() for x in frame[symbol_col].dropna() if str(x).strip()})
        return self._universe_cache

    def _load_master(self):
        if self._master_cache is not None:
            return self._master_cache
        response = requests.get(DHAN_MASTER_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        frame = pd.read_csv(io.BytesIO(response.content), low_memory=False)
        frame.columns = [str(c).strip().upper().replace(" ", "_") for c in frame.columns]

        def find(*names):
            for name in names:
                if name in frame.columns:
                    return name
            return None

        # Dhan has used several master-file schemas. Support both old and new names.
        sid_col = find("SECURITY_ID", "SECURITYID", "SEM_SMST_SECURITY_ID", "SMST_SECURITY_ID", "SECURITY_ID_NEW")
        symbol_col = find("SYMBOL_NAME", "SYMBOL", "TRADING_SYMBOL", "SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "CUSTOM_SYMBOL")
        exchange_col = find("EXCH_ID", "EXCHANGE", "SEM_EXM_EXCH_ID", "EXCHANGE_ID")
        segment_col = find("SEGMENT", "EXCHANGE_SEGMENT", "SEM_SEGMENT", "SEM_EXM_EXCH_ID")
        instrument_col = find("INSTRUMENT", "INSTRUMENT_TYPE", "INSTRUMENT_NAME", "SEM_INSTRUMENT_NAME")
        if not sid_col or not symbol_col:
            # Do not crash the entire Streamlit page with a redacted RuntimeError.
            self.last_error = f"Dhan master schema not recognized. Available columns: {', '.join(frame.columns[:20])}"
            self._master_cache = {}
            return self._master_cache

        if exchange_col:
            values = frame[exchange_col].astype(str).str.upper().str.strip()
            frame = frame[values.isin({"NSE", "NSE_EQ", "NSE EQUITY", "NSE_EQ_CASH", "NSE"}) | values.str.contains("NSE", na=False)]
        if instrument_col:
            values = frame[instrument_col].astype(str).str.upper().str.strip()
            frame = frame[values.isin({"EQUITY", "EQ", "E", "ES", "STOCK"}) | values.str.contains("EQUITY", na=False) | values.isin({"NAN", "NONE"})]

        master = {}
        for _, row in frame.iterrows():
            symbol = str(row.get(symbol_col, "")).strip().upper()
            sid = str(row.get(sid_col, "")).strip()
            if not symbol or symbol.lower() == "nan" or not sid or sid.lower() == "nan":
                continue
            if symbol not in master:
                master[symbol] = {"security_id": sid, "exchange": "NSE_EQ", "instrument": "EQUITY", "symbol": symbol}
        self._master_cache = master
        if not master:
            self.last_error = "Dhan master loaded but no NSE equity instruments were found."
        return master

    def stock_scan(self):
        if self._scan_cache["df"] is not None and time_module.monotonic() - self._scan_cache["at"] < 15:
            return self._scan_cache["df"]
        try:
            universe, master = self._load_universe(), self._load_master()
            configs = [master[s] for s in universe if s in master]
        except Exception as exc:
            self.last_error = str(exc)
            return pd.DataFrame()
        rows = []
        for start in range(0, len(configs), 1000):
            batch = configs[start:start + 1000]
            try:
                quotes = self.dhan.ohlc({"NSE_EQ": [int(c["security_id"]) for c in batch]})
            except Exception as exc:
                self.last_error = str(exc)
                continue
            for c in batch:
                try:
                    item = self._quote_item(quotes, "NSE_EQ", c["security_id"])
                    if not item:
                        continue
                    ohlc = item.get("ohlc") or {}
                    ltp = float(item.get("last_price"))
                    op = float(ohlc.get("open")) if ohlc.get("open") not in (None, "", 0) else None
                    pdc = float(ohlc.get("close")) if ohlc.get("close") not in (None, "", 0) else None
                    refs = self.reference_levels(c)
                    pdc = pdc or refs.get("pdc")
                    orb = self.opening_range(c) or {}
                    orh, orl = orb.get("high"), orb.get("low")
                    rows.append({"Symbol": c["symbol"], "LTP": ltp, "Open": op, "PDC": pdc, "Today %": ((ltp-pdc)/pdc*100 if pdc else None), "1W %": ((ltp-refs["week_close"])/refs["week_close"]*100 if refs.get("week_close") else None), "1M %": ((ltp-refs["month_close"])/refs["month_close"]*100 if refs.get("month_close") else None), "3M %": ((ltp-refs["quarter_close"])/refs["quarter_close"]*100 if refs.get("quarter_close") else None), "ORB High": orh, "ORB Low": orl, "Buy condition": "BUY" if orh is not None and ltp > orh else "WAIT"})
                except Exception:
                    continue
        result = pd.DataFrame(rows)
        self._scan_cache = {"at": time_module.monotonic(), "df": result}
        return result

    def snapshot(self):
        result = {"warning": None, "data_status": "Disconnected", "daily_pnl": self.daily_pnl(), "pdc": None, "week_close": None, "month_close": None, "quarter_close": None, "nifty500_ltp": None, "nifty500_change": None, "nifty500_change_pct": None}
        if not self.dhan.ready:
            result["warning"] = "Enter Dhan credentials in Streamlit Secrets."
            return result
        try:
            c = self._cfg()
            result["nifty500_ltp"] = self._nifty500_ltp()
            result.update(self.reference_levels(c))
            if result["pdc"] is not None:
                result["nifty500_change"] = result["nifty500_ltp"] - result["pdc"]
                result["nifty500_change_pct"] = result["nifty500_change"] / result["pdc"] * 100
            result["data_status"] = "Connected to Dhan"
        except Exception as exc:
            result["warning"] = f"Dhan data error: {exc}"
            self.last_error = str(exc)
        return result

    def setup_table(self, direction):
        frame = self.stock_scan()
        if frame.empty:
            return pd.DataFrame([{"Status": self.last_error or "No stock data returned."}])
        if direction == "BUY":
            return frame[frame["Buy condition"] == "BUY"].sort_values("Today %", ascending=False).reset_index(drop=True)
        return frame[frame["Buy condition"] != "BUY"].sort_values("Today %", ascending=True).reset_index(drop=True)

    def market_table(self):
        return pd.DataFrame()

    def today_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) == str(self._today)])

    def past_positions(self):
        return pd.DataFrame([p for p in self.state["positions"] if str(p.get("date")) != str(self._today)])

    def strategy_markdown(self):
        return "**Universe:** NSE Nifty 500 constituent list. **Live LTP/Open/PDC:** Dhan OHLC market-feed only. **ORB:** Dhan 1-minute candles from 09:15–09:30 IST. **Buy condition:** LTP > ORB High. **Alignment:** Today %, 1W %, 1M %, 3M %. Paper trading only."

    def config_table(self):
        return pd.DataFrame([{"Name": k, **v} for k, v in self.config.items()])
