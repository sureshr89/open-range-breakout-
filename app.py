import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine

st.set_page_config(page_title="ORB Strategy Dashboard", page_icon="📈", layout="wide")
refresh_count = st_autorefresh(interval=15_000, key="orb_live_refresh")
st.title("📈 ORB Strategy Dashboard")
st.caption("Dhan market-data driven • Paper-trading logic • Auto-refresh every 15 seconds")

with st.sidebar:
    st.header("Dhan connection")
    client_id = str(st.secrets.get("DHAN_CLIENT_ID", "") or "").strip()
    access_token = str(st.secrets.get("DHAN_ACCESS_TOKEN", "") or "").strip()
    if client_id and access_token:
        st.success("Dhan secrets loaded")
    else:
        st.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN")
    st.divider()
    st.subheader("Risk controls")
    per_trade_risk = st.number_input("Risk / trade (₹)", min_value=2000, max_value=2500, value=2250, step=50)
    max_day_loss = st.number_input("Max daily loss (₹)", min_value=5000, max_value=5000, value=5000, step=500)
    target_rr = st.number_input("Target R:R", min_value=1.0, max_value=5.0, value=2.0, step=0.5)
    st.toggle("Paper trading", value=True, disabled=True)
    st.divider()
    st.caption("No order-placement API is enabled. This dashboard is paper-trading only.")
    st.caption(f"Refresh #{refresh_count} • every 15 seconds")

@st.cache_resource(show_spinner=False)
def get_engine(cid, token, risk, max_loss, rr):
    return ORBEngine(cid, token, risk, max_loss, rr)

engine = get_engine(client_id, access_token, per_trade_risk, max_day_loss, target_rr)

st.subheader("NIFTY 500 market overview")
all_stocks = engine.stock_scan()
index = engine.index_metrics() or {}

ltp = index.get("LTP")
pdc = index.get("PDC")
change = ((ltp - pdc) / pdc * 100) if ltp is not None and pdc else None
values = [ltp, pdc, change, engine.daily_pnl()]
metric_cols = st.columns(4)
labels = ["NIFTY 500 index LTP", "NIFTY 500 index PDC", "NIFTY 500 today % vs PDC", "Daily P&L"]
for col, label, value, kind in zip(metric_cols, labels, values, ["price", "price", "change", "price"]):
    if value is None:
        display = "—"
    elif kind == "change":
        display = f"{value:+.2f}%"
    else:
        display = f"{value:,.2f}"
    col.metric(label, display)

st.subheader("NIFTY 500 alignment scanner")
st.caption("NIFTY 500 constituents • Dhan live LTP • quote/session date • one scan per 15-second refresh")
if all_stocks.empty:
    st.info("No Dhan LTP quotes returned in this scan. The next refresh will retry.")
else:
    st.dataframe(all_stocks, use_container_width=True, hide_index=True)

st.subheader("Buy setups")
st.dataframe(engine.setup_table("BUY"), use_container_width=True, hide_index=True)
st.subheader("Sell setups")
sell_setups = engine.setup_table("SELL")
if "Buy condition" in sell_setups.columns:
    sell_setups = sell_setups[sell_setups["Buy condition"] == "SELL"].reset_index(drop=True)
st.dataframe(sell_setups, use_container_width=True, hide_index=True)

st.subheader("Today's position details")
st.dataframe(engine.today_positions(), use_container_width=True, hide_index=True)
st.subheader("Past position details")
st.dataframe(engine.past_positions(), use_container_width=True, hide_index=True)

with st.expander("📘 Complete strategy", expanded=False):
    st.markdown(engine.strategy_markdown())
with st.expander("⚙️ Symbol / security configuration", expanded=False):
    st.dataframe(engine.config_table(), use_container_width=True, hide_index=True)
if engine.last_error:
    st.error(f"Live data diagnostic: {engine.last_error}")
else:
    st.success("Data source: Dhan • Universe: NIFTY 500 • One scan per 15-second refresh")
st.caption(f"Dashboard time: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')} IST")
