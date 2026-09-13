import streamlit as st
from datetime import datetime
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

engine = ORBEngine(client_id, access_token, per_trade_risk, max_day_loss, target_rr)

# Always render the dashboard, even if a remote NSE/Dhan request fails.
# The engine itself caches the scan during this Streamlit run.
try:
    all_stocks = engine.stock_scan()
except Exception as exc:
    all_stocks = __import__("pandas").DataFrame()
    engine.last_error = f"Scanner error: {type(exc).__name__}: {exc}"


def mean_value(column):
    if all_stocks.empty or column not in all_stocks.columns:
        return None
    values = __import__("pandas").to_numeric(all_stocks[column], errors="coerce").dropna()
    return float(values.mean()) if not values.empty else None

basket_ltp = mean_value("LTP")
basket_pdc = mean_value("PDC")
basket_change_pct = mean_value("Today %")

cols = st.columns(4)
metrics = [
    ("NIFTY 500 basket LTP", basket_ltp, None),
    ("NIFTY 500 average PDC", basket_pdc, None),
    ("NIFTY 500 today % vs PDC", basket_change_pct, "%"),
    ("Daily P&L", engine.daily_pnl(), None),
]
for col, (label, value, suffix) in zip(cols, metrics):
    display = "—" if value is None else (f"{value:+.2f}%" if suffix == "%" else f"{value:,.2f}")
    col.metric(label, display)

st.subheader("NIFTY 500 alignment scanner")
st.caption("NIFTY 500 constituents • Dhan live LTP • one scan per 15-second refresh")
if all_stocks.empty:
    st.info("Waiting for NIFTY 500 data…")
else:
    st.dataframe(all_stocks, use_container_width=True, hide_index=True)

st.subheader("Buy setups")
st.dataframe(engine.setup_table("BUY"), use_container_width=True, hide_index=True)
st.subheader("Sell / waiting setups")
st.dataframe(engine.setup_table("SELL"), use_container_width=True, hide_index=True)
st.subheader("Today's position details")
st.dataframe(engine.today_positions(), use_container_width=True, hide_index=True)
st.subheader("Past position details")
st.dataframe(engine.past_positions(), use_container_width=True, hide_index=True)

with st.expander("📘 Complete strategy", expanded=False):
    st.markdown(engine.strategy_markdown())
with st.expander("⚙️ Symbol / security configuration", expanded=False):
    st.dataframe(engine.config_table(), use_container_width=True, hide_index=True)

if engine.last_error:
    st.warning(engine.last_error)
else:
    st.success("Data source: Dhan • Universe: NIFTY 500 • One scan per 15-second refresh")
st.caption(f"Dashboard time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
