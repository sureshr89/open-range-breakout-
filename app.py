import streamlit as st
from datetime import datetime, time
from pathlib import Path
import pandas as pd
from engine import ORBEngine

st.set_page_config(page_title="ORB Strategy Dashboard", page_icon="📈", layout="wide")

st.title("📈 ORB Strategy Dashboard")
st.caption("Dhan market-data driven • Nifty 500 equity universe • Paper-trading logic")

with st.sidebar:
    st.header("Dhan connection")
    client_id = st.text_input("Dhan Client ID", value=st.secrets.get("DHAN_CLIENT_ID", ""), type="password")
    access_token = st.text_input("Dhan Access Token", value=st.secrets.get("DHAN_ACCESS_TOKEN", ""), type="password")
    st.divider()
    st.subheader("Risk controls")
    per_trade_risk = st.number_input("Risk / trade (₹)", min_value=2000, max_value=2500, value=2250, step=50)
    max_day_loss = st.number_input("Max daily loss (₹)", min_value=5000, max_value=5000, value=5000, step=500)
    target_rr = st.number_input("Target R:R", min_value=1.0, max_value=5.0, value=2.0, step=0.5)
    paper_mode = st.toggle("Paper trading", value=True, disabled=True)
    st.divider()
    st.caption("No order-placement API is enabled in this build.")

engine = ORBEngine(client_id=client_id, access_token=access_token, risk_per_trade=per_trade_risk,
                   max_daily_loss=max_day_loss, target_rr=target_rr)

if st.button("🔄 Refresh market data", type="primary", use_container_width=True):
    st.cache_data.clear()

# Read the Dhan quote/historical snapshot on each Streamlit rerun; intraday refresh can be user-triggered.
status = engine.snapshot()

cols = st.columns(6)
metrics = [
    ("Nifty 500", status.get("nifty500_ltp"), status.get("nifty500_change")),
    ("PDC", status.get("pdc"), None),
    ("1W Close", status.get("week_close"), None),
    ("1M Close", status.get("month_close"), None),
    ("3M Close", status.get("quarter_close"), None),
    ("Daily P&L", status.get("daily_pnl"), None),
]
for c, (label, value, delta) in zip(cols, metrics):
    c.metric(label, "—" if value is None else f"{value:,.2f}", delta=None if delta is None else f"{delta:+.2f}")

st.subheader("Live market data")
market_df = engine.market_table()
st.dataframe(market_df, use_container_width=True, hide_index=True)

st.subheader("Buy setups")
buy_df = engine.setup_table("BUY")
st.dataframe(buy_df, use_container_width=True, hide_index=True)

st.subheader("Sell setups")
sell_df = engine.setup_table("SELL")
st.dataframe(sell_df, use_container_width=True, hide_index=True)

st.subheader("Today's position details")
today = engine.today_positions()
st.dataframe(today, use_container_width=True, hide_index=True)

st.subheader("Past position details")
past = engine.past_positions()
st.dataframe(past, use_container_width=True, hide_index=True)

with st.expander("📘 Complete strategy", expanded=False):
    st.markdown(engine.strategy_markdown())

with st.expander("⚙️ Symbol / security configuration", expanded=False):
    st.dataframe(engine.config_table(), use_container_width=True, hide_index=True)

if status.get("warning"):
    st.warning(status["warning"])
else:
    st.success(f"Data status: {status.get('data_status', 'OK')}")

st.caption(f"Dashboard time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
