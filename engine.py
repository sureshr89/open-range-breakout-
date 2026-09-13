from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from pathlib import Path
import json
import math
import requests
import pandas as pd

IST = "Asia/Kolkata"
API = "https://api.dhan.co/v2"

# Keep the app under three source files. The universe/security IDs can be extended in this map/config.
# Nifty 500 index securityId is commonly exposed as 390; verify against your current Dhan scrip master if needed.
DEFAULT_CONFIG = {
    "NIFTY_500": {"security_id": "390", "exchange": "IDX_I", "instrument": "INDEX", "symbol": "NIFTY 500"},
    "NIFTY": {"security_id": "13", "exchange": "IDX_I", "instrument": "INDEX", "symbol": "NIFTY"},
}

@dataclass
class RiskState:
    risk_per_trade: float = 2250.0
    max_daily_loss: float = 5000.0
    target_rr: float = 2.0

class DhanClient:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id.strip()
        self.access_token = access_token.strip()
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

    def _post(self, path: str, payload: dict):
        if not self.ready:
            raise RuntimeError("Dhan Client ID and Access Token are required.")
        r = self.session.post(f"{API}{path}", json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "failure":
            raise RuntimeError(data.get("remarks") or data.get("message") or str(data))
        return data

    def ltp(self, securities: dict):
        return self._post("/marketfeed/ltp", securities)

    def daily(self, security_id: str, exchange: str, instrument: str, from_date: str, to_date: str):
        payload = {
            "securityId": str(security_id), "exchangeSegment": exchange, "instrument": instrument,
            "expiryCode": 0, "oi": False, "fromDate": from_date, "toDate": to_date
        }
        return self._post("/charts/historical", payload)

    def intraday(self, security_id: str, exchange: str, instrument: str, interval: str, from_dt: str, to_dt: str):
        payload = {
            "securityId": str(security_id), "exchangeSegment": exchange, "instrument": instrument,
            "interval": interval, "oi": False, "fromDate": from_dt, "toDate": to_dt
        }
        return self._post("/charts/intraday", payload)


def _to_df(data: dict) -> pd.DataFrame:
    if not data or "timestamp" not in data:
        return pd.DataFrame()
    n = len(data["timestamp"])
    out = pd.DataFrame({k: data.get(k, [None] * n) for k in ["timestamp", "open", "high", "low", "close", "volume"]})
    out["timestamp"] = pd.to_datetime(out["timestamp"], unit="s", errors="coerce")
    return out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


class ORBEngine:
    def __init__(self, client_id: str, access_token: str, risk_per_trade=2250, max_daily_loss=5000, target_rr=2.0):
        self.dhan = DhanClient(client_id, access_token)
        self.risk = RiskState(risk_per_trade, max_daily_loss, target_rr)
        self.cache_file = Path("orb_state.json")
        self.config = dict(DEFAULT_CONFIG)
        self.last_error = ""
        self._today = date.today()
        self._load_state()

    def _load_state(self):
        if self.cache_file.exists():
            try:
                self.state = json.loads(self.cache_file.read_text())
            except Exception:
                self.state = {"positions": []}
        else:
            self.state = {"positions": []}
        self.state.setdefault("positions", [])

    def _save_state(self):
        try:
            self.cache_file.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def _today_range(self):
        d = self._today
        return d.strftime("%Y-%m-%d")

    def _index_df(self):
        c = self.config["NIFTY_500"]
        d = self.dhan.daily(c["security_id"], c["exchange"], c["instrument"],
                             (self._today - timedelta(days=100)).strftime("%Y-%m-%d"),
                             (self._today + timedelta(days=1)).strftime("%Y-%m-%d"))
        return _to_df(d)

    def _nifty500_ltp(self):
        c = self.config["NIFTY_500"]
        q = self.dhan.ltp({c["exchange"]: [int(c["security_id"])]})
        try:
            return float(q["data"][c["exchange"]][str(c["security_id"])] ["last_price"])
        except Exception:
            # Some responses key by integer; be tolerant.
            return float(q["data"][c["exchange"]][int(c["security_id"])] ["last_price"])

    def reference_levels(self, symbol_cfg: dict):
        df = self.dhan.daily(symbol_cfg["security_id"], symbol_cfg["exchange"], symbol_cfg["instrument"],
                              (self._today - timedelta(days=120)).strftime("%Y-%m-%d"),
                              (self._today + timedelta(days=1)).strftime("%Y-%m-%d"))
        d = _to_df(df)
        if d.empty:
            return {"pdc": None, "week_close": None, "month_close": None, "quarter_close": None}
        closes = d["close"].astype(float).reset_index(drop=True)
        return {
            "pdc": float(closes.iloc[-2]) if len(closes) >= 2 else None,
            "week_close": float(closes.iloc[-6]) if len(closes) >= 6 else float(closes.iloc[0]),
            "month_close": float(closes.iloc[-22]) if len(closes) >= 22 else float(closes.iloc[0]),
            "quarter_close": float(closes.iloc[-66]) if len(closes) >= 66 else float(closes.iloc[0]),
        }

    def opening_range(self, symbol_cfg: dict):
        now = datetime.now()
        start = f"{self._today:%Y-%m-%d} 09:15:00"
        end = f"{self._today:%Y-%m-%d} 09:30:00"
        data = self.dhan.intraday(symbol_cfg["security_id"], symbol_cfg["exchange"], symbol_cfg["instrument"], "1", start, end)
        d = _to_df(data)
        if d.empty:
            return None
        # Use 09:15 through 09:29 inclusive. Exclude the 09:30 bar itself.
        d = d[(d.timestamp.dt.time >= time(9, 15)) & (d.timestamp.dt.time < time(9, 30))]
        if d.empty:
            return None
        return {"high": float(d.high.max()), "low": float(d.low.min()), "start": d.timestamp.iloc[0], "end": d.timestamp.iloc[-1]}

    def _condition_state(self, ltp, refs, opening, sector_pdc=None, sector_ltp=None, nifty500_ltp=None):
        if opening is None or ltp is None or refs["pdc"] is None:
            return {"breakout": False, "all_filters": False}
        # Breakout trigger is evaluated by the caller using the live 1-minute bar / prior bar close.
        common = {
            "LTP > PDC": ltp > refs["pdc"],
            "LTP > 1W close": ltp > refs["week_close"] if refs["week_close"] is not None else False,
            "LTP > 1M close": ltp > refs["month_close"] if refs["month_close"] is not None else False,
            "LTP > 3M close": ltp > refs["quarter_close"] if refs["quarter_close"] is not None else False,
            "Sector LTP > sector PDC": (sector_ltp is not None and sector_pdc is not None and sector_ltp > sector_pdc),
            "Nifty 500 > 0": (nifty500_ltp is not None and nifty500_ltp > 0),
        }
        return common

    def setup_table(self, direction: str):
        # The architecture is ready for a full 500-symbol scanner. Until a Dhan scrip master / symbol list is supplied,
        # dashboard shows the core benchmark setup and clearly reports configuration status rather than inventing IDs.
        rows = []
        try:
            c = self.config["NIFTY_500"]
            ltp = self._nifty500_ltp()
            refs = self.reference_levels(c)
            op = self.opening_range(c)
            cond = self._condition_state(ltp, refs, op, nifty500_ltp=ltp)
            if direction == "BUY":
                base = bool(ltp and op and ltp > op["high"])
                all_ok = base and all(cond.values())
            else:
                base = bool(ltp and op and ltp < op["low"])
                reverse = {
                    "LTP < PDC": ltp < refs["pdc"] if refs["pdc"] is not None else False,
                    "LTP < 1W close": ltp < refs["week_close"] if refs["week_close"] is not None else False,
                    "LTP < 1M close": ltp < refs["month_close"] if refs["month_close"] is not None else False,
                    "LTP < 3M close": ltp < refs["quarter_close"] if refs["quarter_close"] is not None else False,
                    "Sector LTP < sector PDC": True,
                    "Nifty 500 > 0 reversed": ltp < 0,
                }
                all_ok = base and all(reverse.values())
                cond = reverse
            rows.append({"Symbol": c["symbol"], "Direction": direction, "LTP": ltp,
                         "ORB High": op["high"] if op else None, "ORB Low": op["low"] if op else None,
                         "Signal": "READY" if all_ok else "WAIT",
                         "Filters": " | ".join([k for k,v in cond.items() if v])})
        except Exception as e:
            self.last_error = str(e)
            rows.append({"Symbol": "—", "Direction": direction, "LTP": None, "ORB High": None,
                         "ORB Low": None, "Signal": "CONFIG/DATA ERROR", "Filters": str(e)})
        return pd.DataFrame(rows)

    def snapshot(self):
        result = {"warning": None, "data_status": "Disconnected", "daily_pnl": 0.0,
                  "pdc": None, "week_close": None, "month_close": None, "quarter_close": None,
                  "nifty500_ltp": None, "nifty500_change": None}
        if not self.dhan.ready:
            result["warning"] = "Enter Dhan Client ID and Access Token in the sidebar, or put them in Streamlit secrets as DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN."
            return result
        try:
            c = self.config["NIFTY_500"]
            result["nifty500_ltp"] = self._nifty500_ltp()
            refs = self.reference_levels(c)
            result.update(refs)
            if refs["pdc"] is not None:
                result["nifty500_change"] = result["nifty500_ltp"] - refs["pdc"]
            result["daily_pnl"] = self.daily_pnl()
            result["data_status"] = "Connected to Dhan"
        except Exception as e:
            result["warning"] = f"Dhan data error: {e}"
            self.last_error = str(e)
        return result

    def market_table(self):
        rows = []
        try:
            c = self.config["NIFTY_500"]
            ltp = self._nifty500_ltp()
            refs = self.reference_levels(c)
            op = self.opening_range(c)
            rows.append({"Instrument": c["symbol"], "LTP": ltp, "PDC": refs["pdc"],
                         "1W Close": refs["week_close"], "1M Close": refs["month_close"],
                         "3M Close": refs["quarter_close"], "ORB High": op["high"] if op else None,
                         "ORB Low": op["low"] if op else None})
        except Exception as e:
            rows.append({"Instrument": "NIFTY 500", "LTP": None, "PDC": None, "1W Close": None,
                         "1M Close": None, "3M Close": None, "ORB High": None, "ORB Low": None})
        return pd.DataFrame(rows)

    def _trade_qty(self, entry, sl):
        risk_per_share = abs(entry - sl)
        if risk_per_share <= 0:
            return 0
        return max(1, int(self.risk.risk_per_trade // risk_per_share))

    def proposed_trade(self, direction, entry, orb_high, orb_low):
        sl = orb_low if direction == "BUY" else orb_high
        risk = abs(entry - sl)
        target = entry + self.risk.target_rr * risk if direction == "BUY" else entry - self.risk.target_rr * risk
        qty = self._trade_qty(entry, sl)
        return {"direction": direction, "entry": entry, "sl": sl, "target": target, "qty": qty,
                "max_risk": qty * risk}

    def today_positions(self):
        today = str(self._today)
        rows = [p for p in self.state["positions"] if str(p.get("date")) == today]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["date","symbol","direction","entry","sl","target","qty","exit","pnl","status"])

    def past_positions(self):
        today = str(self._today)
        rows = [p for p in self.state["positions"] if str(p.get("date")) != today]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["date","symbol","direction","entry","sl","target","qty","exit","pnl","status"])

    def daily_pnl(self):
        today = str(self._today)
        return float(sum(float(p.get("pnl", 0)) for p in self.state["positions"] if str(p.get("date")) == today))

    def strategy_markdown(self):
        return f"""
**Opening range:** 09:15–09:29, 1-minute candles. The ORB high/low is the high/low of those 15 one-minute candles.

**BUY:** a 1-minute candle crosses from below to above the ORB high, then LTP > previous-day close, LTP > 1-week close, LTP > 1-month close, LTP > 3-month close, sector LTP > sector previous-day close, and Nifty 500 > 0.

**SELL:** the exact reverse: 1-minute candle crosses from above to below the ORB low, LTP < previous-day close, LTP < 1-week close, LTP < 1-month close, LTP < 3-month close, sector LTP < sector previous-day close, and Nifty 500 < 0.

**Trading window:** entries only 09:30–13:00 IST. No new entries after 13:00. All open positions must be closed by 14:55 IST.

**Position rule:** maximum one open position at a time.

**Risk:** max daily loss ₹{self.risk.max_daily_loss:,.0f}; target risk per trade ₹{self.risk.risk_per_trade:,.0f}; target is {self.risk.target_rr}:1. Quantity = floor(risk budget ÷ per-share stop distance), with a minimum of 1 share.

**Safety:** this build does not place live orders. It calculates the signal/risk state and stores paper-trading position records locally in `orb_state.json`.
"""

    def config_table(self):
        return pd.DataFrame([{"Name": k, **v} for k,v in self.config.items()])
