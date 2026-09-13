from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine, market_open

st.set_page_config(page_title='ORB Strategy Dashboard', page_icon='📈', layout='wide')
refresh_count = st_autorefresh(interval=15_000, key='orb_live_refresh')
IST = ZoneInfo('Asia/Kolkata')


def safe_timestamp(value):
    """Render any supported timestamp value without crashing Streamlit."""
    if value is None:
        return 'None'
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = pd.to_datetime(value, errors='coerce')
            if pd.isna(dt):
                return 'None'
            if hasattr(dt, 'to_pydatetime'):
                dt = dt.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)
        return dt.strftime('%Y-%m-%d %H:%M:%S IST')
    except Exception:
        return 'Unavailable'


st.title('📈 ORB Strategy Dashboard')
st.caption('NIFTY 500 • Paper trading only • 15-second refresh')

with st.sidebar:
    st.header('Dhan connection')
    client_id = str(st.secrets.get('DHAN_CLIENT_ID', '') or '').strip()
    token = str(st.secrets.get('DHAN_ACCESS_TOKEN', '') or '').strip()
    if client_id and token:
        st.success('Dhan secrets loaded')
    else:
        st.error('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')

    st.divider()
    st.subheader('Risk controls')
    risk = st.number_input('Risk / trade (₹)', 1.0, 100000.0, 2250.0, 50.0)
    max_loss = st.number_input('Max daily loss (₹)', 1.0, 1000000.0, 5000.0, 500.0)
    rr = st.number_input('Target R:R', 1.0, 10.0, 2.0, 0.5)
    st.checkbox('Paper trading', value=True, disabled=True)
    st.caption('Live order placement is disabled.')
    st.caption(f'Refresh #{refresh_count} • every 15 seconds')


@st.cache_resource(show_spinner=False)
def get_engine(cid, tok, r, loss, target):
    return ORBEngine(cid, tok, r, loss, target)


engine = get_engine(client_id, token, risk, max_loss, rr)

if not market_open():
    st.warning('NSE market closed — live Dhan requests are disabled.')
    frame = pd.DataFrame({'Status': ['NSE market closed — live Dhan requests are disabled.']})
else:
    with st.spinner('Loading NIFTY 500 quotes in one shared batch...'):
        frame = engine.stock_scan()

last_error = getattr(engine, 'last_error', None)
if last_error:
    st.error(f'Data diagnostic: {last_error}')

last_successful_quote = getattr(engine, 'last_successful_quote', None)
source = 'Market closed' if not market_open() else ('Dhan' if last_successful_quote else 'Unavailable')

st.subheader('NIFTY 500 index')
st.info('Index data unavailable — no second Dhan request is made. The scanner uses the single shared equity quote response.')
cols = st.columns(4)
cols[0].metric('Index LTP', 'Index data unavailable')
cols[1].metric('Source', source)
cols[2].metric('Last successful quote', safe_timestamp(last_successful_quote))
cols[3].metric('Trading allowed', 'YES' if engine.can_trade() else 'NO')

st.subheader('NIFTY 500 scanner')
if frame.empty or 'LTP' not in frame.columns:
    st.warning('No stock quotes available. Check Dhan access, market hours, or cooldown diagnostic.')
else:
    st.metric('Stocks with quotes', len(frame))
    st.dataframe(frame, hide_index=True, use_container_width=True)

for title, side in [('Buy setups', 'BUY'), ('Sell setups', 'SELL')]:
    st.subheader(title)
    st.dataframe(engine.setup_table(side), hide_index=True, use_container_width=True)

st.subheader("Today's positions")
st.dataframe(engine.today_positions(), hide_index=True, use_container_width=True)
st.subheader('Past positions')
st.dataframe(engine.past_positions(), hide_index=True, use_container_width=True)

with st.expander('📘 Strategy'):
    st.markdown(engine.strategy_markdown())
with st.expander('⚙️ Configuration'):
    st.dataframe(engine.config_table(), hide_index=True, use_container_width=True)

st.caption(f"Dashboard time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
