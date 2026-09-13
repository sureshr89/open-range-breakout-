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
st.caption('Dhan market data • NSE NIFTY 500 • Paper trading only • 15-second refresh')


def find_nifty500_security_id(headers):
    """Resolve the real Dhan IDX_I ID for the NSE NIFTY 500 index. Never guess IDs."""
    urls = [
        'https://images.dhan.co/api-data/api-scrip-master-detailed.csv',
        'https://images.dhan.co/api-data/api-scrip-master.csv',
    ]
    id_keys = {'SECURITY_ID', 'SECURITYID', 'SEM_SMST_SECURITY_ID', 'SEM_SECURITY_ID'}
    name_keys = {
        'SEM_CUSTOM_SYMBOL', 'CUSTOM_SYMBOL', 'SYMBOL_NAME', 'SYMBOL',
        'DISPLAY_NAME', 'UNDERLYING_SYMBOL', 'INSTRUMENT_NAME',
        'SEM_TRADING_SYMBOL', 'TRADING_SYMBOL'
    }
    segment_keys = {'EXCH_ID', 'EXCHANGE', 'SEGMENT', 'EXCH_SEG', 'SEM_SEGMENT', 'SEM_EXM_EXCH_ID'}

    for url in urls:
        try:
            r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=(8, 60))
            r.raise_for_status()
            reader = csv.DictReader(StringIO(r.text))
            for row in reader:
                clean = lambda value: ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
                normalized_values = [clean(v) for v in row.values()]
                normalized_names = [clean(row.get(k, '')) for k in name_keys]
                normalized_segments = [clean(row.get(k, '')) for k in segment_keys]
                is_nifty500 = any(v in {'NIFTY500', 'NIFTY500INDEX'} for v in normalized_names)
                if not is_nifty500:
                    is_nifty500 = 'NIFTY500' in ' '.join(normalized_values)
                is_index = not normalized_segments or any(
                    v in {'IDXI', 'INDEX', 'NSEINDEX', 'NSE'} for v in normalized_segments
                )
                if not (is_nifty500 and is_index):
                    continue
                for key, value in row.items():
                    if key.upper() in id_keys and str(value).strip().isdigit():
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
            timeout=(8, 20),
        )
        r.raise_for_status()
        data = r.json()
        bucket = (data.get('data') or {}).get('IDX_I') or {}
        item = bucket.get(str(sid)) or bucket.get(sid) or {}
        ltp = item.get('last_price') or item.get('ltp')
        o = item.get('ohlc') or {}
        if ltp is None or float(ltp) <= 0:
            return None, f'Dhan returned no valid quote for exact NIFTY 500 security ID {sid}'
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

# Mandatory market-condition gate: resolve and validate NIFTY 500 BEFORE scanning stocks.
index_data, index_error = fetch_nifty500_index(client_id, token)
if not index_data:
    st.error('🚫 TRADING BLOCKED — NIFTY 500 index price unavailable')
    st.warning(index_error or 'NIFTY 500 quote unavailable')
    st.info('No BUY, SELL, signal generation, or paper trade is allowed until NIFTY 500 is available.')
    st.stop()

frame = engine.stock_scan()

if engine.last_error:
    st.error(f'Data diagnostic: {engine.last_error}')

if index_data:
    a, b, c, d = st.columns(4)
    a.metric('NIFTY 500 Index', f"₹{index_data['ltp']:,.2f}")
    b.metric('Index Open', f"₹{float(index_data['open']):,.2f}" if index_data['open'] is not None else '—')
    c.metric('Index High', f"₹{float(index_data['high']):,.2f}" if index_data['high'] is not None else '—')
    d.metric('Index Low', f"₹{float(index_data['low']):,.2f}" if index_data['low'] is not None else '—')
    st.caption(f"NSE NIFTY 500 • Exact Dhan IDX_I security ID: {index_data['security_id']}")

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
