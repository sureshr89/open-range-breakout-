import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine

st.set_page_config(page_title="ORB Strategy Dashboard", page_icon="📈", layout="wide")
refresh_count = st_autorefresh(interval=15_000, key="orb_live_refresh")
st.title("📈 ORB Strategy Dashboard")
st.caption("Dhan market data • Paper trading only • Refresh every 15 seconds")

with st.sidebar:
    st.header("Dhan connection")
    client_id = str(st.secrets.get("DHAN_CLIENT_ID", "") or "").strip()
    token = str(st.secrets.get("DHAN_ACCESS_TOKEN", "") or "").strip()
    st.success("Dhan secrets loaded") if client_id and token else st.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN")
    st.divider()
    st.subheader("Risk controls")
    risk = st.number_input("Risk / trade (₹)", min_value=1.0, value=2250.0, step=50.0)
    max_loss = st.number_input("Max daily loss (₹)", min_value=1.0, value=5000.0, step=500.0)
    rr = st.number_input("Target R:R", min_value=1.0, max_value=5.0, value=2.0, step=0.5)
    st.checkbox("Paper trading", value=True, disabled=True)
    st.caption("Live order placement is intentionally disabled.")
    st.caption(f"Refresh #{refresh_count} • every 15 seconds")

@st.cache_resource(show_spinner=False)
def get_engine(cid, tok, r, loss, target):
    return ORBEngine(cid, tok, r, loss, target)

engine = get_engine(client_id, token, risk, max_loss, rr)
frame = engine.stock_scan()

st.subheader("NIFTY 500 market overview")
if frame.empty or "LTP" not in frame:
    st.warning("No market quotes available yet.")
else:
    st.metric("Stocks with quotes", len(frame))
    st.metric("Average change", f"{frame['Today %'].dropna().mean():+.2f}%" if frame['Today %'].notna().any() else "—")
    st.metric("Daily P&L", f"₹{engine.daily_pnl():,.2f}")
    st.metric("Trading allowed", "YES" if engine.can_trade() else "NO")

st.subheader("NIFTY 500 scanner")
st.dataframe(frame, use_container_width=True, hide_index=True)
st.subheader("Buy setups")
st.dataframe(engine.setup_table("BUY"), use_container_width=True, hide_index=True)
st.subheader("Sell setups")
st.dataframe(engine.setup_table("SELL"), use_container_width=True, hide_index=True)
st.subheader("Today's positions")
st.dataframe(engine.today_positions(), use_container_width=True, hide_index=True)
st.subheader("Past positions")
st.dataframe(engine.past_positions(), use_container_width=True, hide_index=True)

with st.expander("📘 Strategy", expanded=False): st.markdown(engine.strategy_markdown())
with st.expander("⚙️ Instrument configuration", expanded=False): st.dataframe(engine.config_table(), use_container_width=True, hide_index=True)
if engine.last_error: st.error(f"Data diagnostic: {engine.last_error}")
else: st.success("Data source: Dhan • Paper mode • No live orders")
st.caption(f"Dashboard time: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')} IST")
