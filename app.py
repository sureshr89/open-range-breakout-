import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from io import StringIO
import csv
from streamlit_autorefresh import st_autorefresh
from engine import ORBEngine

st.set_page_config(page_title='ORB Strategy Dashboard', page_icon='📈', layout='wide')
refresh_count = st_autorefresh(interval=15_000, key='orb_live_refresh')
st.title('📈 ORB Strategy Dashboard')
st.caption('Dhan market data • NIFTY 500 • Paper trading only • 15-second refresh')


def find_nifty500_security_id(headers):
    """Find the exact Dhan IDX_I security ID; never guess 13/28."""
    urls = [
        'https://images.dhan.co/api-data/api-scrip-master.csv',
        'https://images.dhan.co/api-data/api-scrip-master-detailed.csv',
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=(5, 20))
            r.raise_for_status()
            text = r.text
            reader = csv.DictReader(StringIO(text))
            for row in reader:
                normalized = ' '.join(str(v or '').upper().replace('_', ' ') for v in row.values())
                segment = ' '.join(str(row.get(k, '') or '').upper() for k in ('EXCH_ID', 'EXCHANGE', 'SEGMENT', 'EXCH_SEG'))
                if 'IDX_I' not in segment and 'INDEX' not in segment:
                    continue
                if 'NIFTY 500' not in normalized and 'NIFTY500' not in normalized:
                    continue
                for key, value in row.items():
                    if key.upper() in ('SECURITY_ID', 'SECURITYID', 'SEM_SMST_SECURITY_ID') and str(value).strip().isdigit():
                        return int(str(value).strip()), None
        except Exception:
            continue
    return None, 'Could not find exact NIFTY 500 security ID in Dhan instrument master'


def fetch_nifty500_index(client_id, token):
    if not client_id or not token:
        return None, 'Missing Dhan credentials'
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'access-token': token,
        'client-id': client_id,
    }
    try:
        sid, sid_error = find_nifty500_security_id(headers)
        if sid is None:
            return None, sid_error
        r = requests.post(
            'https://api.dhan.co/v2/marketfeed/ohlc',
            headers=headers,
            json={'IDX_I': [sid]},
            timeout=(5, 15),
        )
        r.raise_for_status()
        data = r.json()
        bucket = (data.get('data') or {}).get('IDX_I') or {}
        item = bucket.get(str(sid)) or bucket.get(sid) or {}
        ltp = item.get('last_price') or item.get('ltp')
        o = item.get('ohlc') or {}
        if ltp is None:
            return None, f'Dhan returned no quote for exact NIFTY 500 security ID {sid}'
        return {
            'ltp': float(ltp),
            'open': o.get('open'),
            'high': o.get('high'),
            'low': o.get('low'),
            'close': o.get('close'),
            'security_id': sid,
        }, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


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
index_data, index_error = fetch_nifty500_index(client_id, token)

if engine.last_error:
    st.error(f'Data diagnostic: {engine.last_error}')

if index_data:
    a, b, c, d = st.columns(4)
    a.metric('NIFTY 500 Index', f"₹{index_data['ltp']:,.2f}")
    b.metric('Index Open', f"₹{float(index_data['open']):,.2f}" if index_data['open'] is not None else '—')
    c.metric('Index High', f"₹{float(index_data['high']):,.2f}" if index_data['high'] is not None else '—')
    d.metric('Index Low', f"₹{float(index_data['low']):,.2f}" if index_data['low'] is not None else '—')
    st.caption(f"Exact Dhan NIFTY 500 security ID: {index_data['security_id']}")
else:
    st.warning(f'NIFTY 500 index price unavailable: {index_error}')

if frame.empty or 'LTP' not in frame.columns:
    st.warning('No market quotes available. Check Dhan Data API subscription, token, and market hours.')
else:
    c1, c2, c3, c4 = st.columns(4)
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

with st.expander('📘 Strategy', expanded=False):
    st.markdown(engine.strategy_markdown())
with st.expander('⚙️ Configuration', expanded=False):
    st.dataframe(engine.config_table(), hide_index=True)

if not engine.last_error:
    st.success('Data source: Dhan • Paper mode • No live orders')
st.caption(f"Dashboard time: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')} IST")
