import ccxt
import pandas as pd
import numpy as np
import time
import re
import json
import logging
import os
import sys
from datetime import datetime
from numba import jit
import threading

pd.set_option('future.no_silent_downcasting', True)

# ==============================================================================
# [환경변수 설정]
# ==============================================================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')

if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
    print("ERROR: Missing environment variables!")
    sys.exit(1)

# ==============================================================================
# [전역 변수]
# ==============================================================================
trade_counter = 0
cumulative_pnl = 0.0
bot_capital = 30.0
should_exit = False

# 캐싱
cached_base_data = None
last_cache_time = None

# 동기화 모드
bot_mode = "LIVE"
virtual_position = None
sync_monitor = None

# ==============================================================================
# [로그 설정]
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logging.info("="*80)
logging.info("실전봇 v23 Trailing 버전 시작 (Risk-Free + Trailing Gap)")
logging.info("="*80)

# ==============================================================================
# [동기화 모드 함수]
# ==============================================================================
def load_sync_config():
    """sync_config.json 파일 읽기"""
    config_file = 'sync_config.json'
    if not os.path.exists(config_file):
        return None
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not config.get('enabled', False):
            return None
        return config
    except Exception as e:
        logging.error(f"동기화 설정 읽기 실패: {e}")
        return None

def log_virtual_trade_entry(side, entry_price, initial_sl):
    logging.info("━"*60)
    logging.info(f"[가상 포지션 진입] {side.upper()}")
    logging.info(f"  진입가: {entry_price:.6f}")
    logging.info(f"  초기 SL: {initial_sl:.6f}")
    logging.info("━"*60)

def log_virtual_trade_exit(side, entry_price, exit_price, initial_sl, final_sl, reason):
    if side == 'long':
        pnl_pct = (exit_price / entry_price - 1) * 100
    else:
        pnl_pct = (entry_price / exit_price - 1) * 100
    
    fee_pct = 0.08
    net_pnl_pct = pnl_pct - fee_pct
    
    logging.info("━"*60)
    logging.info(f"[가상 포지션 청산] {side.upper()}")
    logging.info(f"  진입가: {entry_price:.6f} → 청산가: {exit_price:.6f}")
    logging.info(f"  사유: {reason}")
    logging.info(f"  수익률: {net_pnl_pct:+.2f}% (실제 거래 없음)")
    logging.info("━"*60)

# ==============================================================================
# [Numba - Supertrend 계산]
# ==============================================================================
@jit(nopython=True, cache=True)
def _supertrend_kernel(high, low, close, atr_period, multiplier, n):
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    tr[0] = high[0] - low[0]

    atr = np.zeros(n)
    atr[0] = tr[0]
    alpha = 1.0 / atr_period
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]

    hl2 = (high + low) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    final_upper = upper.copy()
    final_lower = lower.copy()
    direction = np.zeros(n, dtype=np.int8)
    supertrend = np.zeros(n)

    if close[0] > upper[0]:
        direction[0] = 1
        supertrend[0] = lower[0]
    else:
        direction[0] = -1
        supertrend[0] = upper[0]

    for i in range(1, n):
        if close[i-1] <= final_upper[i-1]:
            final_upper[i] = min(upper[i], final_upper[i-1])
        else:
            final_upper[i] = upper[i]
        
        if close[i-1] >= final_lower[i-1]:
            final_lower[i] = max(lower[i], final_lower[i-1])
        else:
            final_lower[i] = lower[i]

        if close[i] > final_upper[i-1]:
            direction[i] = 1
        elif close[i] < final_lower[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]

        if direction[i] == 1:
            supertrend[i] = final_lower[i]
        else:
            supertrend[i] = final_upper[i]

    return supertrend, direction

def calculate_supertrend_fast(df, atr_period, multiplier):
    high = df['high'].values.astype(np.float64)
    low = df['low'].values.astype(np.float64)
    close = df['close'].values.astype(np.float64)
    n = len(df)
    st_line, st_dir = _supertrend_kernel(high, low, close, atr_period, multiplier, n)
    return pd.DataFrame({'direction': st_dir, 'value': st_line}, index=df.index)

# ==============================================================================
# [설정]
# ==============================================================================
API_CONFIG = {
    'apiKey': BITGET_API_KEY,
    'secret': BITGET_SECRET_KEY,
    'password': BITGET_PASSPHRASE,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
}

symbol = 'LINK/USDT:USDT'
base_timeframe = '3m'
higher_timeframe = '3m'

trailing_trigger_pct = 3.00
trailing_gap_pct = 1.90
stop_loss_pct = 2.80
be_buffer_pct = 0.2

atr_period = 81
atr_multiplier = 8.1

START_CAPITAL = 30.0
MIN_ENTRY_USDT = 5.0
MAX_ENTRY_USDT = 10000.0
LEVERAGE = 1

CHECK_INTERVAL = 5
MONITOR_INTERVAL = 3
CANDLE_MINUTES = 3

DATA_LOOKBACK = 3000
API_LIMIT = 1000

# ==============================================================================
# [유틸리티 함수]
# ==============================================================================
def fetch_extended_ohlcv(exchange, symbol, timeframe, total_limit=3000):
    all_data = []
    
    data = exchange.fetch_ohlcv(symbol, timeframe, limit=API_LIMIT)
    if not data:
        return []
    
    all_data = data.copy()
    call_count = 1
    
    while len(all_data) < total_limit and call_count < 5:
        oldest_timestamp = all_data[0][0]
        since = oldest_timestamp - (API_LIMIT * 3 * 60 * 1000)
        time.sleep(0.2)
        
        try:
            older_data = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=API_LIMIT)
            if not older_data:
                break
            
            oldest_existing = all_data[0][0]
            new_data = [d for d in older_data if d[0] < oldest_existing]
            
            if not new_data:
                break
            
            all_data = new_data + all_data
            call_count += 1
        except:
            break
    
    result = all_data[-total_limit:] if len(all_data) > total_limit else all_data
    return result

def fetch_ohlcv_with_cache(exchange, symbol, timeframe, total_limit=3000, force_refresh=False):
    global cached_base_data, last_cache_time
    
    now = datetime.now()
    
    if (cached_base_data is None or force_refresh or 
        last_cache_time is None or (now - last_cache_time).total_seconds() > 180):
        
        cached_base_data = fetch_extended_ohlcv(exchange, symbol, timeframe, total_limit)
        last_cache_time = now
        return cached_base_data
    
    try:
        latest_data = exchange.fetch_ohlcv(symbol, timeframe, limit=10)
        
        if latest_data and cached_base_data:
            last_cached_ts = cached_base_data[-1][0]
            new_candles = [d for d in latest_data if d[0] > last_cached_ts]
            
            if new_candles:
                cached_base_data = cached_base_data + new_candles
                if len(cached_base_data) > total_limit:
                    cached_base_data = cached_base_data[-total_limit:]
        
        last_cache_time = now
        return cached_base_data
    except:
        return cached_base_data if cached_base_data else []

def get_precision(exchange, symbol):
    try:
        market = exchange.market(symbol)
        price_prec = market['precision']['price']
        amount_prec = market['precision']['amount']
        
        if price_prec is None or price_prec <= 0 or price_prec > 8:
            price_prec = 4
        if amount_prec is None or amount_prec <= 0 or amount_prec > 8:
            amount_prec = 1
        
        return int(price_prec), int(amount_prec)
    except:
        return 4, 1

def truncate(number, precision):
    if number is None or number == 0:
        return 0
    
    if precision is None or precision <= 0 or precision > 8:
        precision = 4
    
    factor = 10 ** precision
    result = int(number * factor) / factor
    
    if result == 0:
        return round(number, precision)
    
    return result

def get_current_position(exchange, symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if pos['symbol'] == symbol:
                contracts = float(pos['contracts']) if pos['contracts'] else 0
                side = pos['side']
                entry_price = float(pos['entryPrice']) if pos['entryPrice'] else 0
                return {'qty': contracts, 'side': side, 'entry': entry_price}
    except:
        pass
    return {'qty': 0, 'side': None, 'entry': 0}

def cancel_all_orders(exchange, symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        if len(orders) > 0:
            exchange.cancel_all_orders(symbol)
    except:
        pass

def close_position_market(exchange, symbol, position_side, qty):
    try:
        side = 'sell' if position_side == 'long' else 'buy'
        order = exchange.create_market_order(
            symbol, side, qty, 
            params={'reduceOnly': True}
        )
        logging.info(f"시장가 청산 완료: {position_side} {qty}")
        return True
    except:
        logging.error(f"청산 실패")
        return False

# ==============================================================================
# [신호 생성]
# ==============================================================================
def get_signal_with_detail(exchange, symbol, use_cache=True):
    try:
        if use_cache:
            base_data = fetch_ohlcv_with_cache(exchange, symbol, base_timeframe, DATA_LOOKBACK)
        else:
            base_data = fetch_extended_ohlcv(exchange, symbol, base_timeframe, DATA_LOOKBACK)
        
        if len(base_data) < 100:
            return None, None
        
        base_df = pd.DataFrame(base_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        base_df['timestamp'] = pd.to_datetime(base_df['timestamp'], unit='ms')
        base_df.set_index('timestamp', inplace=True)
        
        base_df['prev_open'] = base_df['open'].shift(1)
        base_df['prev_close'] = base_df['close'].shift(1)
        
        base_df['isBullishEngulfing'] = (
            (base_df['prev_close'] < base_df['prev_open']) & 
            (base_df['close'] > base_df['open']) &
            (base_df['close'] > base_df['prev_open']) & 
            (base_df['open'] <= base_df['prev_close'])
        ).astype(bool)
        
        base_df['isBearishEngulfing'] = (
            (base_df['prev_close'] > base_df['prev_open']) & 
            (base_df['close'] < base_df['open']) &
            (base_df['open'] >= base_df['prev_close']) & 
            (base_df['close'] < base_df['prev_open'])
        ).astype(bool)
        
        st_df = calculate_supertrend_fast(base_df, atr_period, atr_multiplier)
        base_df['trend'] = st_df['direction']
        
        if len(base_df) < 2:
            return None, None
        
        signal_candle = base_df.iloc[-2]
        next_open = base_df.iloc[-1]['open']
        
        trend = signal_candle['trend']
        bull_pattern = signal_candle['isBullishEngulfing']
        bear_pattern = signal_candle['isBearishEngulfing']
        
        is_long = (trend == 1) and bull_pattern
        is_short = (trend == -1) and bear_pattern
        
        signal = 'long' if is_long else ('short' if is_short else None)
        
        if signal:
            logging.info(f"신호 발생: {signal.upper()} @ {next_open:.6f}")
        
        return signal, next_open
        
    except Exception as e:
        logging.error(f"신호 생성 에러: {e}")
        return None, None

# ==============================================================================
# [가상 포지션 모니터] - 동기화 모드용
# ==============================================================================
class VirtualPositionMonitor:
    def __init__(self, exchange, symbol, side, entry, initial_sl):
        self.exchange = exchange
        self.symbol = symbol
        self.side = side
        self.entry = entry
        self.initial_sl = initial_sl
        self.current_sl = initial_sl
        self.should_stop = False
        self._last_print = 0
        
        self.trailing_active = False
        self.extreme_price = entry
        
    def check_exit(self):
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = float(ticker.get('last', 0))
            
            now = time.time()
            if now - self._last_print > 60:
                status = "BE" if self.trailing_active else "Wait"
                logging.info(f"[가상] {current_price:.4f} | SL:{self.current_sl:.4f} | {status}")
                self._last_print = now
            
            if current_price == 0:
                return None, None
            
            if self.side == 'long':
                if not self.trailing_active:
                    if current_price >= self.entry * (1 + trailing_trigger_pct / 100):
                        self.trailing_active = True
                        be_price = self.entry * (1 + be_buffer_pct / 100)
                        if self.current_sl < be_price:
                            self.current_sl = be_price
                        self.extreme_price = current_price
                        logging.info(f"[가상] Risk-Free 발동! BE: {be_price:.6f}")
                
                if self.trailing_active:
                    if current_price > self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 - trailing_gap_pct / 100)
                        if new_sl > self.current_sl:
                            self.current_sl = new_sl
                
                if current_price <= self.current_sl:
                    reason = "Risk-Free" if self.trailing_active else "SL"
                    return reason, current_price
            else:
                if not self.trailing_active:
                    if current_price <= self.entry * (1 - trailing_trigger_pct / 100):
                        self.trailing_active = True
                        be_price = self.entry * (1 - be_buffer_pct / 100)
                        if self.current_sl > be_price:
                            self.current_sl = be_price
                        self.extreme_price = current_price
                        logging.info(f"[가상] Risk-Free 발동! BE: {be_price:.6f}")
                
                if self.trailing_active:
                    if current_price < self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 + trailing_gap_pct / 100)
                        if new_sl < self.current_sl:
                            self.current_sl = new_sl
                
                if current_price >= self.current_sl:
                    reason = "Risk-Free" if self.trailing_active else "SL"
                    return reason, current_price
            
            return None, None
        except:
            return None, None
    
    def run(self):
        logging.info(f"[가상 포지션] {self.side.upper()} Entry:{self.entry:.6f} SL:{self.initial_sl:.6f}")
        log_virtual_trade_entry(self.side, self.entry, self.initial_sl)
        
        while not self.should_stop and not should_exit:
            reason, exit_price = self.check_exit()
            
            if reason:
                logging.info(f"[가상] 청산: {reason} @ {exit_price:.6f}")
                log_virtual_trade_exit(
                    side=self.side,
                    entry_price=self.entry,
                    exit_price=exit_price,
                    initial_sl=self.initial_sl,
                    final_sl=self.current_sl,
                    reason=reason
                )
                
                self.should_stop = True
                break
            
            time.sleep(MONITOR_INTERVAL)

# ==============================================================================
# [실제 포지션 모니터]
# ==============================================================================
class PositionMonitor:
    def __init__(self, exchange, symbol, side, entry, initial_sl, qty, order_id, entry_time, entry_amount):
        self.exchange = exchange
        self.symbol = symbol
        self.side = side
        self.entry = entry
        self.initial_sl = initial_sl
        self.current_sl = initial_sl
        self.qty = qty
        self.order_id = order_id
        self.entry_time = entry_time
        self.entry_amount = entry_amount
        self.should_stop = False
        self._last_print = 0
        
        self.trailing_active = False
        self.extreme_price = entry
        
    def check_exit(self):
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = float(ticker.get('last', 0))
            
            now = time.time()
            if now - self._last_print > 30:
                status = "BE" if self.trailing_active else "Wait"
                pnl = ((current_price / self.entry - 1) * 100) if self.side == 'long' else ((self.entry / current_price - 1) * 100)
                logging.info(f"Monitor | {current_price:.4f} | PnL:{pnl:+.2f}% | SL:{self.current_sl:.4f} | {status}")
                self._last_print = now
            
            if current_price == 0:
                return None, None
            
            if self.side == 'long':
                if not self.trailing_active:
                    trigger_price = self.entry * (1 + trailing_trigger_pct / 100)
                    if current_price >= trigger_price:
                        self.trailing_active = True
                        be_price = self.entry * (1 + be_buffer_pct / 100)
                        if self.current_sl < be_price:
                            self.current_sl = be_price
                        self.extreme_price = current_price
                        logging.info(f"Risk-Free activated! BE: {be_price:.6f}")
                
                if self.trailing_active:
                    if current_price > self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 - trailing_gap_pct / 100)
                        if new_sl > self.current_sl:
                            self.current_sl = new_sl
                
                if current_price <= self.current_sl:
                    reason = "Risk-Free" if self.trailing_active else "SL"
                    return reason, current_price
            else:
                if not self.trailing_active:
                    trigger_price = self.entry * (1 - trailing_trigger_pct / 100)
                    if current_price <= trigger_price:
                        self.trailing_active = True
                        be_price = self.entry * (1 - be_buffer_pct / 100)
                        if self.current_sl > be_price:
                            self.current_sl = be_price
                        self.extreme_price = current_price
                        logging.info(f"Risk-Free activated! BE: {be_price:.6f}")
                
                if self.trailing_active:
                    if current_price < self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 + trailing_gap_pct / 100)
                        if new_sl < self.current_sl:
                            self.current_sl = new_sl
                
                if current_price >= self.current_sl:
                    reason = "Risk-Free" if self.trailing_active else "SL"
                    return reason, current_price
            
            return None, None
        except:
            return None, None
    
    def run(self):
        logging.info(f"Monitor Start: {self.side.upper()} Entry:{self.entry:.6f} SL:{self.initial_sl:.6f}")
        
        while not self.should_stop and not should_exit:
            reason, exit_price = self.check_exit()
            
            if reason:
                logging.info(f"Exit: {reason} @ {exit_price:.6f}")
                
                position = get_current_position(self.exchange, self.symbol)
                
                if position['qty'] == 0:
                    self.should_stop = True
                    break
                
                cancel_all_orders(self.exchange, self.symbol)
                time.sleep(0.3)
                
                close_position_market(self.exchange, self.symbol, self.side, self.qty)
                self.should_stop = True
                break
            
            time.sleep(MONITOR_INTERVAL)

# ==============================================================================
# [진입 함수]
# ==============================================================================
def execute_entry_with_trailing(exchange, symbol, side, entry_price):
    global bot_capital
    
    if bot_capital <= MIN_ENTRY_USDT:
        logging.warning(f"Insufficient capital: {bot_capital:.2f}")
        return False, None
    
    entry_amount = min(bot_capital, MAX_ENTRY_USDT)
    
    p_prec, a_prec = get_precision(exchange, symbol)
    qty = truncate((entry_amount * LEVERAGE) / entry_price, a_prec)
    
    try:
        exchange.set_leverage(LEVERAGE, symbol)
        
        order_side = 'buy' if side == 'long' else 'sell'
        
        logging.info(f"Entry: {side.upper()} Amount:{entry_amount:.2f} Qty:{qty}")
        
        entry_order = exchange.create_market_order(symbol, order_side, qty)
        
        actual_entry = entry_order.get('average', 0)
        if actual_entry == 0:
            time.sleep(0.5)
            ticker = exchange.fetch_ticker(symbol)
            actual_entry = ticker['last']
        
        actual_entry = float(actual_entry)
        
        entry_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        order_id = entry_order.get('id', 'N/A')
        
        time.sleep(0.5)
        position = get_current_position(exchange, symbol)
        if position['qty'] == 0:
            return False, None
        
        if side == 'long':
            initial_sl_raw = actual_entry * (1 - stop_loss_pct / 100)
        else:
            initial_sl_raw = actual_entry * (1 + stop_loss_pct / 100)
        
        initial_sl = truncate(initial_sl_raw, p_prec)
        
        logging.info(f"Entry Complete: {actual_entry:.4f} SL:{initial_sl:.4f}")
        
        monitor = PositionMonitor(
            exchange, symbol, side, actual_entry, initial_sl, qty,
            order_id, entry_time_str, entry_amount
        )
        
        monitor_thread = threading.Thread(target=monitor.run, daemon=True)
        monitor_thread.start()
        
        return True, monitor
        
    except Exception as e:
        logging.error(f"Entry failed: {e}")
        return False, None

def execute_virtual_entry(exchange, symbol, side, entry_price):
    """가상 포지션 진입"""
    p_prec, _ = get_precision(exchange, symbol)
    
    if side == 'long':
        initial_sl_raw = entry_price * (1 - stop_loss_pct / 100)
    else:
        initial_sl_raw = entry_price * (1 + stop_loss_pct / 100)
    
    initial_sl = truncate(initial_sl_raw, p_prec)
    
    monitor = VirtualPositionMonitor(exchange, symbol, side, entry_price, initial_sl)
    
    monitor_thread = threading.Thread(target=monitor.run, daemon=True)
    monitor_thread.start()
    
    return monitor

# ==============================================================================
# [메인 루프]
# ==============================================================================
def run_live_bot():
    global bot_capital, should_exit, bot_mode, sync_monitor, virtual_position
    
    exchange = ccxt.bitget(API_CONFIG)
    
    # 동기화 모드 확인
    sync_config = load_sync_config()
    
    if sync_config and sync_config.get('enabled'):
        bot_mode = "SYNC"
    else:
        bot_mode = "LIVE"
    
    print("\n" + "="*70)
    if bot_mode == "SYNC":
        print("  Live Bot v23 Trailing (동기화 모드)")
    else:
        print("  Live Bot v23 Trailing (실전 모드)")
    print("="*70)
    print(f"  Symbol: {symbol}")
    print(f"  Capital: {START_CAPITAL:.2f} USDT")
    print(f"  Trailing Trigger: {trailing_trigger_pct}%")
    print(f"  Trailing Gap: {trailing_gap_pct}%")
    print("="*70 + "\n")
    
    logging.info(f"Bot Start: {symbol} Mode:{bot_mode} Capital:{START_CAPITAL}")
    
    if bot_mode == "SYNC":
        logging.info(f"Sync Config: {sync_config['position_type'].upper()} @ {sync_config['entry_price']:.6f}")
        sync_monitor = execute_virtual_entry(
            exchange, symbol, 
            sync_config['position_type'].lower(), 
            sync_config['entry_price']
        )
    
    last_signal_time = None
    current_monitor = None
    
    while not should_exit:
        try:
            # 동기화 모드에서 가상 포지션이 종료되면 실전으로 전환
            if bot_mode == "SYNC" and sync_monitor and sync_monitor.should_stop:
                logging.info("가상 포지션 종료 - 실전 모드로 전환")
                bot_mode = "LIVE"
                sync_monitor = None
                
                # sync_config.json 업데이트
                try:
                    with open('sync_config.json', 'w', encoding='utf-8') as f:
                        json.dump({'enabled': False}, f)
                except:
                    pass
                
                time.sleep(5)
                continue
            
            # SYNC 모드일 땐 신호 생성 안 함
            if bot_mode == "SYNC":
                time.sleep(CHECK_INTERVAL)
                continue
            
            # 실전 모드
            position = get_current_position(exchange, symbol)
            in_position = position['qty'] > 0
            
            if current_monitor and current_monitor.should_stop:
                logging.info(f"Position closed | Capital: {bot_capital:.2f}")
                current_monitor = None
                time.sleep(10)
                continue
            
            if not in_position and not current_monitor:
                current_time = datetime.now()
                
                signal, entry_price = get_signal_with_detail(exchange, symbol, use_cache=True)
                
                if signal:
                    if last_signal_time is None or (current_time - last_signal_time).total_seconds() > 180:
                        
                        logging.info(f"Signal: {signal.upper()} (Capital: {bot_capital:.2f})")
                        
                        try:
                            cancel_all_orders(exchange, symbol)
                            time.sleep(0.3)
                        except:
                            pass
                        
                        success, monitor = execute_entry_with_trailing(
                            exchange, symbol, signal, entry_price
                        )
                        
                        if success:
                            last_signal_time = current_time
                            current_monitor = monitor
                else:
                    if int(current_time.timestamp()) % 300 < CHECK_INTERVAL:
                        logging.info(f"Waiting | Capital: {bot_capital:.2f}")
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(60)
    
    logging.info("Bot stopped")

# ==============================================================================
# [sync_config.json 생성 헬퍼]
# ==============================================================================
def create_sync_config(position_type, entry_price):
    """동기화 설정 파일 생성"""
    config = {
        'enabled': True,
        'position_type': position_type,
        'entry_price': entry_price
    }
    
    with open('sync_config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    logging.info(f"Sync config created: {position_type} @ {entry_price:.6f}")

# ==============================================================================
# [실행]
# ==============================================================================
if __name__ == '__main__':
    try:
        run_live_bot()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)
