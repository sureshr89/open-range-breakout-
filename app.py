import streamlit as st
from datetime import datetime
import time as time_module
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine

st.set_page_config(page_title="ORB Strategy Dashboard", page_icon="📈", layout="wide")

# Automatic refresh every 15 seconds. No manual refresh button is needed.
refresh_count = st_autorefresh(interval=15_000, key="orb_live_refresh")

st.title("📈 ORB Strategy Dashboard")
st.caption("Dhan market-data driven • Paper-trading logic • Auto-refresh every 15 seconds")

with st.sidebar:
    st.header("Dhan connection")
    client_id = st.secrets.get("DHAN_CLIENT_ID", "").strip()
    access_token = st.secrets.get("DHAN_ACCESS_TOKEN", "").strip()
    if client_id and access_token:
        st.success("Dhan secrets loaded")
    else:
        st.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in Streamlit Secrets")
    st.divider()
    st.subheader("Risk controls")
    per_trade_risk = st.number_input("Risk / trade (₹)", min_value=2000, max_value=2500, value=2250, step=50)
    max_day_loss = st.number_input("Max daily loss (₹)", min_value=5000, max_value=5000, value=5000, step=500)
    target_rr = st.number_input("Target R:R", min_value=1.0, max_value=5.0, value=2.0, step=0.5)
    st.toggle("Paper trading", value=True, disabled=True)
    st.divider()
    st.caption("No order-placement API is enabled. This dashboard is paper-trading only.")
    st.caption(f"Refresh #{refresh_count} • every 15 seconds")

engine = ORBEngine(client_id=client_id, access_token=access_token, risk_per_trade=per_trade_risk,
                   max_daily_loss=max_day_loss, target_rr=target_rr)

# Reuse one LTP response during each 15-second refresh cycle.
_original_ltp = engine.dhan.ltp
_ltp_cache = {"time": 0.0, "response": None}
_ltp_cache_ttl = 15.0

def cached_ltp(securities):
    now = time_module.monotonic()
    if _ltp_cache["response"] is not None and now - _ltp_cache["time"] < _ltp_cache_ttl:
        return _ltp_cache["response"]
    response = _original_ltp(securities)
    _ltp_cache["response"] = response
    _ltp_cache["time"] = now
    return response

engine.dhan.ltp = cached_ltp

status = engine.snapshot()

cols = st.columns(4)
metrics = [
    ("NIFTY market LTP", status.get("nifty500_ltp"), None),
    ("Previous-day close", status.get("pdc"), None),
    ("Today % vs PDC", status.get("nifty500_change_pct"), "%"),
    ("Daily P&L", status.get("daily_pnl"), None),
]
for c, (label, value, suffix) in zip(cols, metrics):
    if value is None:
        display = "—"
    elif suffix == "%":
        display = f"{value:+.2f}%"
    else:
        display = f"{value:,.2f}"
    c.metric(label, display)

st.subheader("Live market data")
st.dataframe(engine.market_table(), use_container_width=True, hide_index=True)
st.subheader("Buy setups")
st.dataframe(engine.setup_table("BUY"), use_container_width=True, hide_index=True)
st.subheader("Sell setups")
st.dataframe(engine.setup_table("SELL"), use_container_width=True, hide_index=True)
st.subheader("Today's position details")
st.dataframe(engine.today_positions(), use_container_width=True, hide_index=True)
st.subheader("Past position details")
st.dataframe(engine.past_positions(), use_container_width=True, hide_index=True)

with st.expander("📘 Complete strategy", expanded=False):
    st.markdown(engine.strategy_markdown())
with st.expander("⚙️ Symbol / security configuration", expanded=False):
    st.dataframe(engine.config_table(), use_container_width=True, hide_index=True)

if status.get("warning"):
    st.warning(status["warning"])
else:
    st.success(f"Data status: {status.get('data_status', 'OK')}")

st.caption(f"Dashboard time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
