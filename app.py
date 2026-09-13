import csv
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from streamlit_autorefresh import st_autorefresh

from engine import ORBEngine

st.set_page_config(page_title='ORB Strategy Dashboard', page_icon='📈', layout='wide')
refresh_count = st_autorefresh(interval=15_000, key='orb_live_refresh')
st.title('📈 ORB Strategy Dashboard')
st.caption('Dhan market data • NSE NIFTY 500 • Paper trading only • 15-second refresh')


def clean(value):
    return ''.join(ch for ch in str(value or '').upper() if ch.isalnum())


def find_nifty500_security_id():
    urls = [
        'https://images.dhan.co/api-data/api-scrip-master-detailed.csv',
        'https://images.dhan.co/api-data/api-scrip-master.csv',
    ]
    id_keys = {'SECURITY_ID', 'SECURITYID', 'SEM_SMST_SECURITY_ID', 'SEM_SECURITY_ID'}
    name_keys = {'SEM_CUSTOM_SYMBOL', 'CUSTOM_SYMBOL', 'SYMBOL_NAME', 'SYMBOL', 'DISPLAY_NAME', 'UNDERLYING_SYMBOL', 'INSTRUMENT_NAME', 'SEM_TRADING_SYMBOL', 'TRADING_SYMBOL'}
    for url in urls:
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=(3, 12))
            response.raise_for_status()
            reader = csv.DictReader(StringIO(response.text))
            for row in reader:
                names = [clean(row.get(k, '')) for k in name_keys]
                values = [clean(v) for v in row.values()]
                if not any(v in {'NIFTY500', 'NIFTY500INDEX'} for v in names + values):
                    continue
                for key, value in row.items():
                    if clean(key) in {clean(k) for k in id_keys} and str(value).strip().isdigit():
                        return int(str(value).strip())
        except Exception:
            continue
    return None


def fetch_dhan(client_id, token):
    if not client_id or not token:
        return None, 'Dhan credentials are missing'
    try:
        sid = find_nifty500_security_id()
        if sid is None:
            return None, 'NIFTY 500 security ID was not found in Dhan master'
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'access-token': token, 'client-id': client_id}
        response = requests.post('https://api.dhan.co/v2/marketfeed/ohlc', headers=headers, json={'IDX_I': [sid]}, timeout=(3, 8))
        response.raise_for_status()
        payload = response.json()
        item = ((payload.get('data') or {}).get('IDX_I') or {}).get(str(sid)) or {}
        ohlc = item.get('ohlc') or {}
        ltp = item.get('last_price') or item.get('ltp') or item.get('LTP')
        if ltp is None or float(ltp) <= 0:
            return None, f'Dhan returned no valid price for security ID {sid}'
        return {'ltp': float(ltp), 'open': ohlc.get('open'), 'high': ohlc.get('high'), 'low': ohlc.get('low'), 'security_id': sid, 'source': 'Dhan'}, None
    except Exception as exc:
        return None, f'{type(exc).__name__}: {exc}'


def fetch_yahoo():
    try:
        data = yf.download('^CRSLDX', period='1d', interval='1m', auto_adjust=False, prepost=False, progress=False, threads=False, timeout=5, multi_level_index=False)
        if data is None or data.empty:
            data = yf.download('^CRSLDX', period='5d', interval='5m', auto_adjust=False, prepost=False, progress=False, threads=False, timeout=5, multi_level_index=False)
        if data is None or data.empty or 'Close' not in data.columns:
            return None, 'Yahoo returned no NIFTY 500 data'
        data = data.dropna(subset=['Close'])
        if data.empty:
            return None, 'Yahoo returned no valid NIFTY 500 close'
        last = data.iloc[-1]
        price = float(last['Close'])
        return {'ltp': price, 'open': float(last['Open']) if pd.notna(last.get('Open')) else None, 'high': float(last['High']) if pd.notna(last.get('High')) else None, 'low': float(last['Low']) if pd.notna(last.get('Low')) else None, 'security_id': None, 'source': 'Yahoo ^CRSLDX'}, None
    except Exception as exc:
        return None, f'{type(exc).__name__}: {exc}'


def fetch_index(client_id, token):
    dhan, dhan_error = fetch_dhan(client_id, token)
    if dhan:
        return dhan, None
    yahoo, yahoo_error = fetch_yahoo()
    if yahoo:
        return yahoo, f'Dhan unavailable; Yahoo fallback active. {dhan_error}'
    return None, f'Dhan: {dhan_error} | Yahoo: {yahoo_error}'


with st.sidebar:
    st.header('Dhan connection')
    client_id = str(st.secrets.get('DHAN_CLIENT_ID', '') or '').strip()
    token = str(st.secrets.get('DHAN_ACCESS_TOKEN', '') or '').strip()
    st.success('Dhan secrets loaded') if client_id and token else st.error('Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN')
    st.divider()
    st.subheader('Risk controls')
    risk = st.number_input('Risk / trade (₹)', min_value=1.0, max_value=100000.0, value=2250.0, step=50.0)
    max_loss = st.number_input('Max daily loss (₹)', min_value=1.0, max_value=1000000.0, value=5000.0, step=500.0)
    rr = st.number_input('Target R:R', min_value=1.0, max_value=10.0, value=2.0, step=0.5)
    st.checkbox('Paper trading', value=True, disabled=True)
    st.caption('Live order placement is disabled.')
    st.caption(f'Refresh #{refresh_count} • every 15 seconds')


@st.cache_resource(show_spinner=False)
def get_engine(cid, tok, r, loss, target):
    return ORBEngine(cid, tok, r, loss, target)


engine = get_engine(client_id, token, risk, max_loss, rr)
st.info('🔄 Loading NIFTY 500 index data...')
index_data, index_warning = fetch_index(client_id, token)

if index_warning:
    st.warning(index_warning)

if index_data is None:
    index_data = {'ltp': None, 'open': None, 'high': None, 'low': None, 'security_id': None, 'source': 'Unavailable'}
    st.error('🚫 NIFTY 500 price unavailable — trading and signals are blocked.')
    frame = pd.DataFrame()
else:
    with st.spinner('📊 Loading NIFTY 500 stock scanner...'):
        try:
            frame = engine.stock_scan()
        except Exception as exc:
            frame = pd.DataFrame()
            st.error(f'Stock scanner failed: {type(exc).__name__}: {exc}')

if getattr(engine, 'last_error', None):
    st.error(f'Data diagnostic: {engine.last_error}')

c1, c2, c3, c4 = st.columns(4)
c1.metric('NIFTY 500 Index', f"₹{index_data['ltp']:,.2f}" if index_data['ltp'] is not None else '—')
c2.metric('Index Open', f"₹{float(index_data['open']):,.2f}" if index_data['open'] is not None else '—')
c3.metric('Index High', f"₹{float(index_data['high']):,.2f}" if index_data['high'] is not None else '—')
c4.metric('Index Low', f"₹{float(index_data['low']):,.2f}" if index_data['low'] is not None else '—')
st.caption(f"NSE NIFTY 500 • Source: {index_data['source']}")

if frame.empty or 'LTP' not in frame.columns:
    st.warning('No stock quotes available. Check Dhan Data API access, token, and market hours.')
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Stocks with quotes', len(frame))
    avg = frame['Today %'].dropna().mean() if 'Today %' in frame and frame['Today %'].notna().any() else None
    c2.metric('Average change', f'{avg:+.2f}%' if avg is not None else '—')
    c3.metric('Realized P&L', f'₹{engine.daily_pnl():,.2f}')
    c4.metric('Trading allowed', 'YES' if engine.can_trade() else 'NO')

st.subheader('NIFTY 500 scanner')
st.dataframe(frame, hide_index=True, use_container_width=True)
for title, side in [('Buy setups', 'BUY'), ('Sell setups', 'SELL')]:
    st.subheader(title)
    try:
        st.dataframe(engine.setup_table(side), hide_index=True, use_container_width=True)
    except Exception as exc:
        st.error(f'{title} failed: {type(exc).__name__}: {exc}')

st.subheader("Today's positions")
st.dataframe(engine.today_positions(), hide_index=True, use_container_width=True)
st.subheader('Past positions')
st.dataframe(engine.past_positions(), hide_index=True, use_container_width=True)

with st.expander('📘 Strategy', expanded=False):
    st.markdown(engine.strategy_markdown())
with st.expander('⚙️ Configuration', expanded=False):
    st.dataframe(engine.config_table(), hide_index=True, use_container_width=True)

st.caption(f"Dashboard time: {datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')} IST")
