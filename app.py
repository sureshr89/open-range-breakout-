import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine

st.set_page_config(page_title='ORB Strategy Dashboard', page_icon='📈', layout='wide')
refresh_count = st_autorefresh(interval=15_000, key='orb_live_refresh')
st.title('📈 ORB Strategy Dashboard')
st.caption('Dhan market data • NIFTY 500 • Paper trading only • 15-second refresh')

with st.sidebar:
    st.header('Dhan connection')
    client_id = str(st.secrets.get('DHAN_CLIENT_ID', '') or '').strip()
    token = str(st.secrets.get('DHAN_ACCESS_TOKEN', '') or '').strip()
    if client_id and token: st.success('Dhan secrets loaded')
    else: st.error('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')
    st.divider(); st.subheader('Risk controls')
    risk = st.number_input('Risk / trade (₹)', min_value=1.0, max_value=100000.0, value=2250.0, step=50.0)
    max_loss = st.number_input('Max daily loss (₹)', min_value=1.0, max_value=1000000.0, value=5000.0, step=500.0)
    rr = st.number_input('Target R:R', min_value=1.0, max_value=10.0, value=2.0, step=0.5)
    st.checkbox('Paper trading', value=True, disabled=True)
    st.caption('Live order placement is disabled in code.')
    st.caption(f'Refresh #{refresh_count} • every 15 seconds')

@st.cache_resource(show_spinner=False)
def get_engine(cid, tok, r, loss, target):
    return ORBEngine(cid, tok, r, loss, target)

engine = get_engine(client_id, token, risk, max_loss, rr)
frame = engine.stock_scan()

if engine.last_error:
    st.error(f'Data diagnostic: {engine.last_error}')

if frame.empty or 'LTP' not in frame.columns:
    st.warning('No market quotes available. Check Dhan Data API subscription, token, and market hours.')
else:
    c1,c2,c3,c4 = st.columns(4)
    c1.metric('Stocks with quotes', len(frame))
    avg = frame['Today %'].dropna().mean() if 'Today %' in frame and frame['Today %'].notna().any() else None
    c2.metric('Average change', f'{avg:+.2f}%' if avg is not None else '—')
    c3.metric('Realized P&L', f'₹{engine.daily_pnl():,.2f}')
    c4.metric('Trading allowed', 'YES' if engine.can_trade() else 'NO')

st.subheader('NIFTY 500 scanner')
st.dataframe(frame, hide_index=True)
st.subheader('Buy setups')
st.dataframe(engine.setup_table('BUY'), hide_index=True)
st.subheader('Sell setups')
st.dataframe(engine.setup_table('SELL'), hide_index=True)
st.subheader("Today's positions")
st.dataframe(engine.today_positions(), hide_index=True)
st.subheader('Past positions')
st.dataframe(engine.past_positions(), hide_index=True)

with st.expander('📘 Strategy', expanded=False): st.markdown(engine.strategy_markdown())
with st.expander('⚙️ Configuration', expanded=False): st.dataframe(engine.config_table(), hide_index=True)
st.success('Data source: Dhan • Paper mode • No live orders') if not engine.last_error else None
st.caption(f"Dashboard time: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')} IST")
