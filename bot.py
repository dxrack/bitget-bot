import ccxt
import pandas as pd
import numpy as np
import time
import re
import json
import logging
import os
import csv
import sys
from datetime import datetime, timedelta
from numba import jit
import threading
import signal

pd.set_option('future.no_silent_downcasting', True)

# ==============================================================================
# [환경변수 설정]
# ==============================================================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')

if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
    print("❌ 환경변수 에러: BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE 필요")
    sys.exit(1)

# ==============================================================================
# [전역 변수 초기화]
# ==============================================================================
trade_counter = 0
cumulative_pnl = 0.0
trade_log_data = []

# 데이터 캐싱용 전역 변수
cached_base_data = None
cached_higher_data = None
last_cache_time = None

# [복리] 봇 전용 자금
bot_capital = None

# 종료 플래그
should_exit = False

# ==============================================================================
# [로그 시스템 초기화 - Railway 호환]
# ==============================================================================
def setup_logging():
    # Railway에서는 stdout으로 로깅
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )
    
    logging.info("="*80)
    logging.info("실전봇 v23 Trailing 버전 시작 (Risk-Free + Trailing Gap)")
    logging.info("로그는 Railway 대시보드에서 확인할 수 있습니다")
    logging.info("="*80)

def log_trade_entry(side, entry_price, qty, initial_sl, order_id, entry_amount, current_capital):
    global trade_counter
    trade_counter += 1
    
    entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    logging.info("━"*60)
    logging.info(f"[진입 #{trade_counter}] {side.upper()}")
    logging.info(f"  시간: {entry_time}")
    logging.info(f"  진입가: {entry_price:.6f}")
    logging.info(f"  수량: {qty}")
    logging.info(f"  진입금액: {entry_amount:.2f} USDT")
    logging.info(f"  봇 자금: {current_capital:.2f} USDT")
    logging.info(f"  초기 SL: {initial_sl:.6f}")
    logging.info(f"  주문ID: {order_id}")
    logging.info("━"*60)

def log_trade_exit(side, entry_price, exit_price, initial_sl, final_sl, reason, 
                   entry_time_str, order_id, entry_amount, new_capital, extreme_price):
    global cumulative_pnl, trade_counter
    
    exit_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if side == 'long':
        pnl_pct = (exit_price / entry_price - 1) * 100
    else:
        pnl_pct = (entry_price / exit_price - 1) * 100
    
    fee_pct = 0.08 * 2
    net_pnl_pct = pnl_pct - fee_pct
    actual_pnl = entry_amount * (net_pnl_pct / 100)
    cumulative_pnl += actual_pnl
    
    logging.info("━"*60)
    logging.info(f"[청산 #{trade_counter}] {side.upper()}")
    logging.info(f"  진입가: {entry_price:.6f} → 청산가: {exit_price:.6f}")
    logging.info(f"  사유: {reason}")
    logging.info(f"  초기SL: {initial_sl:.6f} → 최종SL: {final_sl:.6f}")
    logging.info(f"  {'최고가' if side == 'long' else '최저가'}: {extreme_price:.6f}")
    logging.info(f"  진입금액: {entry_amount:.2f} USDT")
    logging.info(f"  수익률: {net_pnl_pct:+.2f}% | 손익: {actual_pnl:+.2f} USDT")
    logging.info(f"  누적: {cumulative_pnl:+.2f} USDT")
    logging.info(f"  봇 자금: {new_capital:.2f} USDT")
    logging.info("━"*60)
    
    # 메모리에 거래 기록 저장
    trade_log_data.append({
        'number': trade_counter,
        'entry_time': entry_time_str,
        'exit_time': exit_time,
        'side': side.upper(),
        'entry_price': entry_price,
        'exit_price': exit_price,
        'initial_sl': initial_sl,
        'final_sl': final_sl,
        'reason': reason,
        'pnl_pct': net_pnl_pct,
        'pnl_usdt': actual_pnl,
        'cumulative_pnl': cumulative_pnl,
        'entry_amount': entry_amount,
        'bot_capital': new_capital,
        'extreme_price': extreme_price,
        'order_id': order_id
    })

# ==============================================================================
# [1] Numba 엔진 - Supertrend 계산]
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
# [2] 설정]
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
target_timeframe = '3m'
higher_timeframe = '3m'

# ==============================================================================
# [전략 파라미터] - Risk-Free + Trailing Gap 방식
# ==============================================================================
long_trend_req = 1
long_pattern_req = 1
short_trend_req = -1
short_pattern_req = 1

trailing_trigger_pct = 3.00
trailing_gap_pct = 1.90
stop_loss_pct = 2.80
be_buffer_pct = 0.2

atr_period = 81
atr_multiplier = 8.1

# ==============================================================================
# [복리] 핵심 설정
# ==============================================================================
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
# [복리] 봇 자금 관리 함수
# ==============================================================================
def update_bot_capital(entry_amount, pnl_pct):
    global bot_capital
    
    fee_pct = 0.08 * 2
    net_pnl_pct = pnl_pct - fee_pct
    pnl_amount = entry_amount * (net_pnl_pct / 100)
    
    old_capital = bot_capital
    bot_capital += pnl_amount
    
    if bot_capital < MIN_ENTRY_USDT:
        logging.warning(f"⚠️ 봇 자금이 최소 금액 미만: {bot_capital:.2f} < {MIN_ENTRY_USDT}")
    
    logging.info(f"[복리] 자금 업데이트: {old_capital:.2f} → {bot_capital:.2f} USDT (손익: {pnl_amount:+.2f})")
    
    return bot_capital, pnl_amount

def get_entry_amount():
    global bot_capital
    
    if bot_capital <= 0:
        logging.warning("봇 자금 없음")
        return 0.0
    
    entry_amount = bot_capital
    
    if entry_amount < MIN_ENTRY_USDT:
        logging.warning(f"진입금액 {entry_amount:.2f} < 최소 {MIN_ENTRY_USDT} USDT → 진입 불가")
        return 0.0
    
    if entry_amount > MAX_ENTRY_USDT:
        entry_amount = MAX_ENTRY_USDT
        logging.info(f"진입금액 최대치 적용: {MAX_ENTRY_USDT} USDT")
    
    logging.info(f"[복리] 진입금액: {entry_amount:.2f} USDT (봇자금: {bot_capital:.2f})")
    
    return entry_amount

# ==============================================================================
# [캔들 마감 동기화]
# ==============================================================================
def get_seconds_until_candle_close():
    now = datetime.now()
    current_minute = now.minute
    current_second = now.second
    
    minutes_to_next = CANDLE_MINUTES - (current_minute % CANDLE_MINUTES)
    if minutes_to_next == CANDLE_MINUTES:
        minutes_to_next = 0
    
    seconds_remaining = (minutes_to_next * 60) - current_second
    
    if seconds_remaining <= 0:
        seconds_remaining += CANDLE_MINUTES * 60
    
    return seconds_remaining

# ==============================================================================
# [유틸리티]
# ==============================================================================
def fetch_extended_ohlcv(exchange, symbol, timeframe, total_limit=3000):
    all_data = []
    
    logging.debug(f"데이터 수집 시작: {symbol} {timeframe} {total_limit}개")
    
    data = exchange.fetch_ohlcv(symbol, timeframe, limit=API_LIMIT)
    if not data:
        return []
    
    all_data = data.copy()
    
    call_count = 1
    while len(all_data) < total_limit:
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
            
        except Exception as e:
            logging.error(f"API 호출 에러: {e}")
            break
        
        if call_count >= 5:
            break
    
    result = all_data[-total_limit:] if len(all_data) > total_limit else all_data
    logging.debug(f"데이터 수집 완료: {len(result)}개")
    
    return result

def fetch_ohlcv_with_cache(exchange, symbol, timeframe, total_limit=3000, force_refresh=False):
    global cached_base_data, last_cache_time
    
    now = datetime.now()
    
    if (cached_base_data is None or 
        force_refresh or 
        last_cache_time is None or 
        (now - last_cache_time).total_seconds() > 180):
        
        logging.debug("전체 데이터 갱신")
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
                logging.debug(f"캐시 업데이트: +{len(new_candles)}개 캔들")
        
        last_cache_time = now
        return cached_base_data
        
    except Exception as e:
        logging.error(f"캐시 업데이트 실패: {e}")
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
        
    except Exception as e:
        logging.error(f"정밀도 조회 실패: {e}")
        return 4, 1

def truncate(number, precision):
    if number is None or number == 0:
        raise ValueError(f"truncate: 유효하지 않은 숫자 {number}")
    
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
    except Exception as e:
        logging.error(f"포지션 조회 에러: {e}")
    return {'qty': 0, 'side': None, 'entry': 0}

def cancel_all_orders(exchange, symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        if len(orders) == 0:
            return
        exchange.cancel_all_orders(symbol)
        logging.info(f"주문 {len(orders)}개 취소")
    except Exception as e:
        if "No order to cancel" not in str(e):
            logging.debug(f"주문 취소 오류: {e}")

def close_position_market(exchange, symbol, position_side, qty):
    try:
        side = 'sell' if position_side == 'long' else 'buy'
        order = exchange.create_market_order(
            symbol, side, qty, 
            params={'reduceOnly': True}
        )
        logging.info(f"시장가 청산 완료: {position_side} {qty}")
        return True
    except Exception as e:
        logging.error(f"청산 실패: {e}")
        return False

# ==============================================================================
# [신호 생성] - HTF 트렌드 조건 추가
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
        
        if higher_timeframe == base_timeframe:
            higher_df = base_df.copy()
        else:
            higher_data = fetch_extended_ohlcv(exchange, symbol, higher_timeframe, DATA_LOOKBACK)
            higher_df = pd.DataFrame(higher_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            higher_df['timestamp'] = pd.to_datetime(higher_df['timestamp'], unit='ms')
            higher_df.set_index('timestamp', inplace=True)
        
        higher_df['prev_open'] = higher_df['open'].shift(1)
        higher_df['prev_close'] = higher_df['close'].shift(1)
        
        higher_df['isBullishEngulfing'] = (
            (higher_df['prev_close'] < higher_df['prev_open']) & 
            (higher_df['close'] > higher_df['open']) &
            (higher_df['close'] > higher_df['prev_open']) & 
            (higher_df['open'] <= higher_df['prev_close'])
        ).astype(bool)
        
        higher_df['isBearishEngulfing'] = (
            (higher_df['prev_close'] > higher_df['prev_open']) & 
            (higher_df['close'] < higher_df['open']) &
            (higher_df['open'] >= higher_df['prev_close']) & 
            (higher_df['close'] < higher_df['prev_open'])
        ).astype(bool)
        
        prev_bull = higher_df['isBullishEngulfing'].shift(1).fillna(False)
        prev_bear = higher_df['isBearishEngulfing'].shift(1).fillna(False)
        higher_df['bullish_signal'] = higher_df['isBullishEngulfing'] & (~prev_bull)
        higher_df['bearish_signal'] = higher_df['isBearishEngulfing'] & (~prev_bear)
        
        higher_df['bullish_signal'] = higher_df['bullish_signal'].shift(1).fillna(False)
        higher_df['bearish_signal'] = higher_df['bearish_signal'].shift(1).fillna(False)
        
        htf_st_df = calculate_supertrend_fast(higher_df, atr_period, atr_multiplier)
        higher_df['htf_trend'] = htf_st_df['direction'].shift(1)
        
        if base_timeframe != target_timeframe:
            minutes = int(re.findall(r'\d+', target_timeframe)[0])
            resampled = base_df.resample(f'{minutes}T').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 
                'close': 'last', 'volume': 'sum'
            }).dropna()
        else:
            resampled = base_df.copy()
        
        resampled = resampled.join(higher_df[['bullish_signal', 'bearish_signal', 'htf_trend']], how='left')
        resampled['bullish_signal'] = resampled['bullish_signal'].ffill().fillna(False)
        resampled['bearish_signal'] = resampled['bearish_signal'].ffill().fillna(False)
        resampled['htf_trend'] = resampled['htf_trend'].ffill().fillna(0)
        
        st_df = calculate_supertrend_fast(resampled, atr_period, atr_multiplier)
        resampled['ltf_trend'] = st_df['direction']
        
        if len(resampled) < 2:
            return None, None
        
        signal_candle = resampled.iloc[-2]
        next_open = resampled.iloc[-1]['open']
        
        ltf_trend = signal_candle['ltf_trend']
        htf_trend = signal_candle['htf_trend']
        bull_pattern = signal_candle['bullish_signal']
        bear_pattern = signal_candle['bearish_signal']
        
        signal_time = signal_candle.name.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_candle.name, 'strftime') else str(signal_candle.name)
        logging.debug(f"신호분석 | 시간:{signal_time} | LTF:{ltf_trend} | HTF:{htf_trend} | Bull:{bull_pattern} | Bear:{bear_pattern}")
        
        is_long = (ltf_trend == long_trend_req) and \
                  (bear_pattern if long_pattern_req == -1 else bull_pattern) and \
                  (htf_trend == 1)
        
        is_short = (ltf_trend == short_trend_req) and \
                   (bull_pattern if short_pattern_req == 1 else bear_pattern) and \
                   (htf_trend == -1)
        
        signal = 'long' if is_long else ('short' if is_short else None)
        
        if signal:
            logging.info(f"🚨 신호 발생: {signal.upper()} @ {next_open:.6f}")
        
        return signal, next_open
        
    except Exception as e:
        logging.error(f"신호 생성 에러: {e}")
        return None, None

# ==============================================================================
# [포지션 모니터] - Risk-Free + Trailing Gap 방식
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
        self.exit_reason = None
        self._last_print = 0
        self._retry_count = 0
        self._MAX_RETRY = 3
        
        self.trailing_active = False
        self.extreme_price = entry
        
    def check_exit(self):
        try:
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = float(ticker.get('last', 0))
            
            now = time.time()
            if now - self._last_print > 30:
                status = "🔒BE" if self.trailing_active else "⏳대기"
                pnl = ((current_price / self.entry - 1) * 100) if self.side == 'long' else ((self.entry / current_price - 1) * 100)
                logging.info(f"모니터링 | {current_price:.4f} | PnL:{pnl:+.2f}% | SL:{self.current_sl:.4f} | {status}")
                self._last_print = now
            
            logging.debug(f"모니터 | 현재가:{current_price:.6f} | SL:{self.current_sl:.6f} | Trailing:{self.trailing_active}")
            
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
                        
                        logging.info(f"🔒 Risk-Free 발동! 본전가: {be_price:.6f}")
                        self._update_exchange_sl(be_price)
                
                if self.trailing_active:
                    if current_price > self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 - trailing_gap_pct / 100)
                        if new_sl > self.current_sl:
                            old_sl = self.current_sl
                            self.current_sl = new_sl
                            logging.info(f"Trailing SL 업데이트: {old_sl:.6f} → {new_sl:.6f}")
                            self._update_exchange_sl(new_sl)
                
                if current_price <= self.current_sl:
                    reason = "Risk-Free 청산" if self.trailing_active else "초기 손절"
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
                        
                        logging.info(f"🔒 Risk-Free 발동! 본전가: {be_price:.6f}")
                        self._update_exchange_sl(be_price)
                
                if self.trailing_active:
                    if current_price < self.extreme_price:
                        self.extreme_price = current_price
                        new_sl = self.extreme_price * (1 + trailing_gap_pct / 100)
                        if new_sl < self.current_sl:
                            old_sl = self.current_sl
                            self.current_sl = new_sl
                            logging.info(f"Trailing SL 업데이트: {old_sl:.6f} → {new_sl:.6f}")
                            self._update_exchange_sl(new_sl)
                
                if current_price >= self.current_sl:
                    reason = "Risk-Free 청산" if self.trailing_active else "초기 손절"
                    return reason, current_price
            
            return None, None
            
        except Exception as e:
            logging.error(f"모니터 체크 오류: {e}")
            return None, None
    
    def _update_exchange_sl(self, new_sl_price):
        try:
            cancel_all_orders(self.exchange, self.symbol)
            time.sleep(0.3)
            
            p_prec, _ = get_precision(self.exchange, self.symbol)
            sl_price = truncate(new_sl_price, p_prec)
            
            sl_side = 'sell' if self.side == 'long' else 'buy'
            self.exchange.create_order(
                symbol=self.symbol, type='market', side=sl_side, amount=self.qty, price=None,
                params={'stopLossPrice': sl_price, 'reduceOnly': True}
            )
            logging.info(f"거래소 SL 업데이트: {sl_price:.6f}")
        except Exception as e:
            logging.warning(f"거래소 SL 업데이트 실패: {e}")
    
    def _finalize_exit(self, reason, exit_price):
        global bot_capital
        
        if self.side == 'long':
            pnl_pct = (exit_price / self.entry - 1) * 100
        else:
            pnl_pct = (self.entry / exit_price - 1) * 100
        
        new_capital, pnl_amount = update_bot_capital(self.entry_amount, pnl_pct)
        
        log_trade_exit(
            side=self.side,
            entry_price=self.entry,
            exit_price=exit_price,
            initial_sl=self.initial_sl,
            final_sl=self.current_sl,
            reason=reason,
            entry_time_str=self.entry_time,
            order_id=self.order_id,
            entry_amount=self.entry_amount,
            new_capital=new_capital,
            extreme_price=self.extreme_price
        )
        
        logging.info(f"✅ 청산 완료 ({reason}) | 손익: {pnl_amount:+.2f} USDT | 봇자금: {new_capital:.2f} USDT")
    
    def run(self):
        logging.info(f"모니터 시작: {self.side.upper()} 진입:{self.entry:.6f} SL:{self.initial_sl:.6f}")
        
        while not self.should_stop and not should_exit:
            reason, exit_price = self.check_exit()
            
            if reason:
                logging.info(f"청산 감지: {reason} @ {exit_price:.6f}")
                
                position = get_current_position(self.exchange, self.symbol)
                
                if position['qty'] == 0:
                    logging.info(f"거래소에서 이미 청산됨")
                    self.exit_reason = reason
                    self.should_stop = True
                    self._finalize_exit(reason, exit_price)
                    break
                
                cancel_all_orders(self.exchange, self.symbol)
                time.sleep(0.3)
                
                success = close_position_market(self.exchange, self.symbol, self.side, self.qty)
                
                if success:
                    self.exit_reason = reason
                    self.should_stop = True
                    self._finalize_exit(reason, exit_price)
                else:
                    self._retry_count += 1
                    if self._retry_count >= self._MAX_RETRY:
                        position = get_current_position(self.exchange, self.symbol)
                        if position['qty'] == 0:
                            self.exit_reason = reason
                            self.should_stop = True
                            self._finalize_exit(reason, exit_price)
                        else:
                            logging.error(f"포지션 남아있음! 수동 확인 필요!")
                            self.should_stop = True
                        break
                    time.sleep(1)
                    continue
                break
            
            time.sleep(MONITOR_INTERVAL)
        
        logging.info(f"모니터 종료: {self.exit_reason}")

# ==============================================================================
# [진입 함수]
# ==============================================================================
def execute_entry_with_trailing(exchange, symbol, side, entry_price):
    global bot_capital
    
    entry_amount = get_entry_amount()
    
    if entry_amount <= 0:
        logging.warning(f"진입 불가: 봇 자금 부족 ({bot_capital:.2f} USDT)")
        return False, None
    
    p_prec, a_prec = get_precision(exchange, symbol)
    qty = truncate((entry_amount * LEVERAGE) / entry_price, a_prec)
    
    try:
        exchange.set_leverage(LEVERAGE, symbol)
        
        order_side = 'buy' if side == 'long' else 'sell'
        
        logging.info(f"진입 시도: {side.upper()} 금액:{entry_amount:.2f} USDT 수량:{qty}")
        
        entry_start_time = time.time()
        
        entry_order = exchange.create_market_order(symbol, order_side, qty)
        
        if entry_order is None:
            raise Exception("주문 객체가 None")
        
        actual_entry = entry_order.get('average')
        if actual_entry is None or actual_entry == 0:
            time.sleep(0.5)
            if 'id' in entry_order:
                order_detail = exchange.fetch_order(entry_order['id'], symbol)
                actual_entry = order_detail.get('average')
            if actual_entry is None or actual_entry == 0:
                ticker = exchange.fetch_ticker(symbol)
                actual_entry = ticker['last']
        
        actual_entry = float(actual_entry)
        
        slippage = abs(actual_entry - entry_price) / entry_price * 100
        logging.info(f"✅ 진입 완료: {side.upper()} @ {actual_entry:.4f} (슬리피지: {slippage:.3f}%)")
        
        entry_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        order_id = entry_order.get('id', 'N/A')
        
        time.sleep(0.5)
        position = get_current_position(exchange, symbol)
        if position['qty'] == 0:
            logging.warning("포지션 확인 실패!")
            return False, None
        
        if side == 'long':
            initial_sl_raw = actual_entry * (1 - stop_loss_pct / 100)
        else:
            initial_sl_raw = actual_entry * (1 + stop_loss_pct / 100)
        
        initial_sl = truncate(initial_sl_raw, p_prec)
        
        time.sleep(0.3)
        sl_side = 'sell' if side == 'long' else 'buy'
        try:
            exchange.create_order(
                symbol=symbol, type='market', side=sl_side, amount=qty, price=None,
                params={'stopLossPrice': initial_sl, 'reduceOnly': True}
            )
            logging.info(f"초기 SL 주문 설정: {initial_sl:.6f}")
        except Exception as e:
            logging.warning(f"SL 주문 실패: {e}")
        
        total_time = time.time() - entry_start_time
        logging.info(f"진입 처리 시간: {total_time:.2f}초")
        
        log_trade_entry(side, actual_entry, qty, initial_sl, order_id, entry_amount, bot_capital)
        
        monitor = PositionMonitor(
            exchange, symbol, side, actual_entry, initial_sl, qty,
            order_id, entry_time_str, entry_amount
        )
        
        monitor_thread = threading.Thread(target=monitor.run, daemon=True)
        monitor_thread.start()
        
        return True, monitor
        
    except Exception as e:
        logging.error(f"진입 실패: {e}")
        
        try:
            cancel_all_orders(exchange, symbol)
            pos = get_current_position(exchange, symbol)
            if pos['qty'] > 0:
                logging.error(f"포지션 열림! 수동 확인 필요!")
        except:
            pass
        
        return False, None

# ==============================================================================
# [시그널 핸들러]
# ==============================================================================
def signal_handler(sig, frame):
    global should_exit
    logging.info("종료 신호 수신 (SIGTERM)")
    should_exit = True

# ==============================================================================
# [메인 루프]
# ==============================================================================
def run_live_bot():
    global trade_log_file, bot_capital, should_exit
    
    setup_logging()
    
    # 종료 신호 핸들러 등록
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    exchange = ccxt.bitget(API_CONFIG)
    
    bot_capital = START_CAPITAL
    
    print("\n" + "="*70)
    print("  🤖 실전봇 v23 Trailing (Railway 버전)")
    print("="*70)
    print(f"  심볼: {symbol}")
    print(f"  시간대: Base={base_timeframe} | Target={target_timeframe} | HTF={higher_timeframe}")
    print("="*70)
    print(f"  [전략: Risk-Free + Trailing Gap]")
    print(f"  - Trigger: +{trailing_trigger_pct}% 도달 시 본전 보장")
    print(f"  - Gap: 고점/저점 대비 {trailing_gap_pct}% 하락 시 청산")
    print(f"  - 초기 SL: {stop_loss_pct}%")
    print(f"  - Supertrend: ATR({atr_period}) × {atr_multiplier}")
    print("="*70)
    print(f"  [복리 설정]")
    print(f"  - 시작 자금: {START_CAPITAL:.2f} USDT")
    print(f"  - 최소 진입: {MIN_ENTRY_USDT} USDT")
    print(f"  - 최대 진입: {MAX_ENTRY_USDT} USDT")
    print("="*70)
    print(f"  💡 상세 로그는 Railway 대시보드의 Logs에서 확인하세요")
    print("="*70 + "\n")
    
    logging.info(f"봇 시작: {symbol}")
    logging.info(f"전략: Trigger={trailing_trigger_pct}% Gap={trailing_gap_pct}% SL={stop_loss_pct}%")
    logging.info(f"복리: 시작자금={START_CAPITAL}")
    
    last_signal_time = None
    current_monitor = None
    
    while not should_exit:
        try:
            position = get_current_position(exchange, symbol)
            in_position = position['qty'] > 0
            
            if current_monitor and current_monitor.should_stop:
                logging.info(f"청산 완료 | 봇자금: {bot_capital:.2f} USDT")
                current_monitor = None
                time.sleep(10)
                continue
            
            if not in_position and not current_monitor:
                current_time = datetime.now()
                
                seconds_to_close = get_seconds_until_candle_close()
                
                if seconds_to_close <= 10 and seconds_to_close > 0:
                    logging.info(f"캔들 마감 {seconds_to_close}초 전, 대기 중...")
                    
                    time.sleep(seconds_to_close + 1.5)
                    
                    signal, entry_price = get_signal_with_detail(exchange, symbol, use_cache=False)
                    
                    if signal:
                        if last_signal_time is None or (current_time - last_signal_time).total_seconds() > 180:
                            
                            logging.info(f"{signal.upper()} 신호! (봇자금: {bot_capital:.2f} USDT)")
                            
                            try:
                                cancel_all_orders(exchange, symbol)
                                time.sleep(0.3)
                            except:
                                pass
                            
                            success, monitor = execute_entry_with_trailing(
                                exchange, symbol, signal, entry_price
                            )
                            
                            if success:
                                last_signal_time = datetime.now()
                                current_monitor = monitor
                    
                    continue
                
                signal, entry_price = get_signal_with_detail(exchange, symbol, use_cache=True)
                
                if signal:
                    if last_signal_time is None or (current_time - last_signal_time).total_seconds() > 180:
                        
                        logging.info(f"{signal.upper()} 신호! (봇자금: {bot_capital:.2f} USDT)")
                        
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
                        next_close = get_seconds_until_candle_close()
                        logging.info(f"대기 중 | 봇자금: {bot_capital:.2f} USDT | 마감: {next_close}초")
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"시스템 에러: {e}")
            time.sleep(60)
    
    # 종료 처리
    total_return = (bot_capital / START_CAPITAL - 1) * 100
    
    print("\n" + "="*50)
    print("  봇 종료")
    print("="*50)
    print(f"  시작 자금: {START_CAPITAL:.2f} USDT")
    print(f"  최종 봇 자금: {bot_capital:.2f} USDT")
    print(f"  총 수익률: {total_return:+.2f}%")
    print(f"  총 거래: {trade_counter}회")
    print("="*50)
    
    logging.info(f"봇 종료: 시작={START_CAPITAL:.2f} 최종={bot_capital:.2f} 수익률={total_return:+.2f}%")
    logging.info(f"총 거래 수: {trade_counter}")

# ==============================================================================
# [실행]
# ==============================================================================
if __name__ == '__main__':
    try:
        run_live_bot()
    except Exception as e:
        logging.error(f"치명적 에러: {e}")
        sys.exit(1)
