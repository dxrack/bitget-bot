"""
Bitget Bot v24.3 - 웹소켓 + Google Sheets + 동기화 모드 + 텔레그램
- 실시간 캔들 데이터 수신 (웹소켓)
- Google Sheets에서 코인별 설정 읽기
- 동기화 모드: 가상 포지션으로 실시간 테스트와 맞춤
- 텔레그램 알림: 진입/청산/동기화 알림
- 최대 20개 코인 동시 운영

v24.3 - 텔레그램 알림 추가
"""

import ccxt
import asyncio
import websockets
import json
import logging
import os
import sys
import gspread
import aiohttp
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np
import time

# ==============================================================================
# [로그 설정]  
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==============================================================================
# [환경변수]
# ==============================================================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
GOOGLE_SHEETS_ID = os.environ.get('GOOGLE_SHEETS_ID', '1fbPwI6F3hELseD1CKDktngR47jsvTvIAbwvfAa6RIsI')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')

# 텔레그램
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
    logging.error("ERROR: Missing Bitget API credentials!")
    sys.exit(1)

# ==============================================================================
# [전역 변수]
# ==============================================================================
candle_data = defaultdict(list)
coin_configs = {}
is_running = True

# ==============================================================================
# [텔레그램 알림]
# ==============================================================================
class TelegramNotifier:
    def __init__(self):
        self.token = TELEGRAM_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        
        if self.enabled:
            logging.info("텔레그램 알림 활성화됨")
        else:
            logging.info("텔레그램 알림 비활성화 (토큰/ChatID 없음)")
    
    async def send(self, message):
        """비동기 텔레그램 메시지 전송"""
        if not self.enabled:
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        logging.info(f"텔레그램 전송 완료")
                    else:
                        logging.warning(f"텔레그램 전송 실패: {resp.status}")
        except Exception as e:
            logging.error(f"텔레그램 에러: {e}")
    
    def send_sync(self, message):
        """동기 텔레그램 메시지 전송 (asyncio 외부에서 사용)"""
        if not self.enabled:
            return
        
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logging.info(f"텔레그램 전송 완료")
        except Exception as e:
            logging.error(f"텔레그램 에러: {e}")

# 전역 텔레그램 인스턴스
telegram = TelegramNotifier()

# ==============================================================================
# [Google Sheets 연동]
# ==============================================================================
class GoogleSheetsManager:
    def __init__(self):
        self.client = None
        self.sheet = None
        self.last_fetch_time = None
        self.cache_duration = 60
        
    def connect(self):
        try:
            if GOOGLE_CREDENTIALS:
                creds_dict = json.loads(GOOGLE_CREDENTIALS)
            else:
                if os.path.exists('google_credentials.json'):
                    with open('google_credentials.json', 'r') as f:
                        creds_dict = json.load(f)
                else:
                    logging.error("Google Credentials not found!")
                    return False
            
            scopes = [
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive'
            ]
            
            credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            self.client = gspread.authorize(credentials)
            self.sheet = self.client.open_by_key(GOOGLE_SHEETS_ID).worksheet('시트1')
            
            logging.info("Google Sheets 연결 성공!")
            return True
            
        except Exception as e:
            logging.error(f"Google Sheets 연결 실패: {e}")
            return False
    
    def get_coin_configs(self, force_refresh=False):
        global coin_configs
        
        now = datetime.now()
        
        if not force_refresh and self.last_fetch_time:
            if (now - self.last_fetch_time).total_seconds() < self.cache_duration:
                return coin_configs
        
        try:
            all_data = self.sheet.get_all_records()
            
            new_configs = {}
            for row in all_data:
                if str(row.get('활성화', 'N')).upper() == 'Y':
                    symbol = row.get('거래쌍', '')
                    if symbol:
                        status = str(row.get('현재상태', 'WAITING')).upper().strip()
                        if status not in ['WAITING', 'SYNC_LONG', 'SYNC_SHORT']:
                            status = 'WAITING'
                        
                        sync_entry_raw = row.get('동기화진입가', '')
                        sync_entry = 0.0
                        if sync_entry_raw and str(sync_entry_raw).strip():
                            try:
                                sync_entry = float(sync_entry_raw)
                            except:
                                sync_entry = 0.0
                        
                        new_configs[symbol] = {
                            'name': row.get('코인명', ''),
                            'symbol': symbol,
                            'stop_loss_pct': float(row.get('손절 %', 2.8)),
                            'trailing_trigger_pct': float(row.get('트레일링 시작%', 3.0)),
                            'trailing_gap_pct': float(row.get('트레일링 스톱%', 1.9)),
                            'be_buffer_pct': float(row.get('BE 버퍼%', 0.2)),
                            'atr_period': int(row.get('슈퍼트렌드 기간', 81)),
                            'atr_multiplier': float(row.get('슈퍼트렌드 배수', 8.1)),
                            'timeframe': row.get('진입시간봉', '3m'),
                            'entry_condition': row.get('진입조건', ''),
                            'capital': 30.0,
                            'leverage': 1,
                            'status': status,
                            'sync_entry_price': sync_entry
                        }
                        logging.info(f"코인 로드: {symbol} | 상태:{status} | 동기화진입가:{sync_entry}")
            
            coin_configs = new_configs
            self.last_fetch_time = now
            
            logging.info(f"총 {len(coin_configs)}개 코인 활성화됨")
            return coin_configs
            
        except Exception as e:
            logging.error(f"시트 읽기 실패: {e}")
            return coin_configs
    
    def update_status(self, symbol, new_status):
        try:
            all_data = self.sheet.get_all_records()
            for idx, row in enumerate(all_data):
                if row.get('거래쌍', '') == symbol:
                    headers = self.sheet.row_values(1)
                    if '현재상태' in headers:
                        col_idx = headers.index('현재상태') + 1
                        row_idx = idx + 2
                        self.sheet.update_cell(row_idx, col_idx, new_status)
                        logging.info(f"[{symbol}] 시트 상태 업데이트: {new_status}")
                    return True
            return False
        except Exception as e:
            logging.error(f"시트 업데이트 실패: {e}")
            return False

# ==============================================================================
# [Supertrend 계산]
# ==============================================================================
def calculate_supertrend(candles, atr_period, multiplier):
    if len(candles) < atr_period + 10:
        return None, None
    
    data = np.array(candles, dtype=np.float64)
    high = data[:, 2]
    low = data[:, 3]
    close = data[:, 4]
    n = len(close)
    
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    
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
    
    if close[0] > upper[0]:
        direction[0] = 1
    else:
        direction[0] = -1
    
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
    
    return direction[-1], direction[-2] if len(direction) > 1 else direction[-1]

# ==============================================================================
# [캔들 패턴 감지]
# ==============================================================================
def detect_engulfing(candles):
    if len(candles) < 3:
        return None
    
    prev_candle = candles[-3]
    signal_candle = candles[-2]
    
    prev_open = prev_candle[1]
    prev_close = prev_candle[4]
    curr_open = signal_candle[1]
    curr_close = signal_candle[4]
    
    is_bullish = (
        prev_close < prev_open and
        curr_close > curr_open and
        curr_close > prev_open and
        curr_open <= prev_close
    )
    
    is_bearish = (
        prev_close > prev_open and
        curr_close < curr_open and
        curr_open >= prev_close and
        curr_close < prev_open
    )
    
    if is_bullish:
        return 'bullish'
    elif is_bearish:
        return 'bearish'
    return None

# ==============================================================================
# [신호 생성]
# ==============================================================================
def generate_signal(symbol, config):
    global candle_data
    
    candles = candle_data.get(symbol, [])
    
    if len(candles) < 101:
        return None, None
        
    # 완성된 캔들만 사용 (마지막은 진행 중이므로 제외)
    completed_candles = candles[:-1]
    
    
    current_trend, prev_trend = calculate_supertrend(
        completed_candles,   # ← 완성된 캔들만!
        config['atr_period'], 
        config['atr_multiplier']
    )
    
    if current_trend is None:
        return None, None
    
    # 패턴 감지
    pattern = detect_engulfing(completed_candles)  # ← 완성된 캔들만!
    
    if pattern is None:
        return None, None
    
     # 진입가는 현재 캔들 시가
    entry_price = candles[-1][1]
    
    # 신호 판단
    if current_trend == 1 and pattern == 'bullish':
        logging.info(f"[{symbol}] LONG 신호! 트렌드:상승 패턴:Bullish @ {entry_price}")
        return 'long', entry_price
    
    if current_trend == -1 and pattern == 'bearish':
        logging.info(f"[{symbol}] SHORT 신호! 트렌드:하락 패턴:Bearish @ {entry_price}")
        return 'short', entry_price
    
    return None, None

# ==============================================================================
# [정밀도 헬퍼]
# ==============================================================================
def get_precision(exchange, symbol):
    try:
        market = exchange.market(symbol)
        price_prec = market.get('precision', {}).get('price')
        if price_prec is None or price_prec <= 0 or price_prec > 8:
            price_prec = 4
        amount_prec = market.get('precision', {}).get('amount')
        if amount_prec is None or amount_prec <= 0 or amount_prec > 8:
            amount_prec = 2
        return int(price_prec), int(amount_prec)
    except:
        return 4, 2

def truncate(value, precision):
    if value is None or precision is None:
        return 0
    try:
        precision = max(0, min(int(precision), 8))
        factor = 10 ** precision
        return int(float(value) * factor) / factor
    except:
        return 0

# ==============================================================================
# [가상 포지션 관리 - 동기화 모드용]
# ==============================================================================
class VirtualPositionManager:
    
    def __init__(self, symbol, config, sheets_manager):
        self.symbol = symbol
        self.config = config
        self.sheets_manager = sheets_manager
        
        self.side = None
        self.entry_price = 0
        self.current_sl = 0
        self.trailing_active = False
        self.extreme_price = 0
        self.is_active = False
        self._last_log_time = 0
        
    def start_sync(self, side, entry_price):
        self.side = side
        self.entry_price = entry_price
        self.trailing_active = False
        
        sl_pct = self.config['stop_loss_pct'] / 100
        if side == 'long':
            self.current_sl = entry_price * (1 - sl_pct)
            self.extreme_price = entry_price
        else:
            self.current_sl = entry_price * (1 + sl_pct)
            self.extreme_price = entry_price
        
        self.is_active = True
        
        # 로그
        logging.info("━" * 60)
        logging.info(f"[{self.symbol}] 🔄 동기화 시작 (가상 포지션)")
        logging.info(f"  방향: {side.upper()}")
        logging.info(f"  진입가: {entry_price:.6f}")
        logging.info(f"  초기 SL: {self.current_sl:.6f}")
        logging.info("━" * 60)
        
        # 텔레그램 알림
        name = self.config.get('name', self.symbol)
        msg = f"🔄 <b>동기화 시작</b>\n\n"
        msg += f"코인: {name}\n"
        msg += f"방향: {side.upper()}\n"
        msg += f"진입가: {entry_price:.4f}\n"
        msg += f"초기 SL: {self.current_sl:.4f}"
        telegram.send_sync(msg)
        
    def check_and_update(self, current_price):
        if not self.is_active:
            return False, None
        
        if current_price is None or current_price <= 0:
            return False, None
        
        now = time.time()
        if now - self._last_log_time > 60:
            status = "BE활성" if self.trailing_active else "대기"
            logging.info(f"[{self.symbol}] [가상] 현재가:{current_price:.4f} | SL:{self.current_sl:.4f} | {status}")
            self._last_log_time = now
        
        trigger_pct = self.config['trailing_trigger_pct'] / 100
        gap_pct = self.config['trailing_gap_pct'] / 100
        be_buffer = self.config['be_buffer_pct'] / 100
        
        should_close = False
        close_reason = ""
        
        if self.side == 'long':
            if not self.trailing_active:
                if current_price >= self.entry_price * (1 + trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 + be_buffer)
                    if self.current_sl < be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] [가상] Risk-Free 활성화! BE:{be_price:.6f}")
                    
                    # 텔레그램 알림
                    name = self.config.get('name', self.symbol)
                    msg = f"🛡️ <b>BE 활성화 (가상)</b>\n\n"
                    msg += f"코인: {name}\n"
                    msg += f"BE가: {be_price:.4f}"
                    telegram.send_sync(msg)
            
            if self.trailing_active:
                if current_price > self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 - gap_pct)
                    if new_sl > self.current_sl:
                        self.current_sl = new_sl
            
            if current_price <= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        else:
            if not self.trailing_active:
                if current_price <= self.entry_price * (1 - trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 - be_buffer)
                    if self.current_sl > be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] [가상] Risk-Free 활성화! BE:{be_price:.6f}")
                    
                    # 텔레그램 알림
                    name = self.config.get('name', self.symbol)
                    msg = f"🛡️ <b>BE 활성화 (가상)</b>\n\n"
                    msg += f"코인: {name}\n"
                    msg += f"BE가: {be_price:.4f}"
                    telegram.send_sync(msg)
            
            if self.trailing_active:
                if current_price < self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 + gap_pct)
                    if new_sl < self.current_sl:
                        self.current_sl = new_sl
            
            if current_price >= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        if should_close:
            self._close_virtual(close_reason, current_price)
            return True, close_reason
        
        return False, None
    
    def _close_virtual(self, reason, exit_price):
        if self.side == 'long':
            pnl_pct = (exit_price / self.entry_price - 1) * 100
        else:
            pnl_pct = (self.entry_price / exit_price - 1) * 100
        
        # 로그
        logging.info("━" * 60)
        logging.info(f"[{self.symbol}] 🔄 동기화 완료 (가상 청산)")
        logging.info(f"  방향: {self.side.upper()}")
        logging.info(f"  진입가: {self.entry_price:.6f} → 청산가: {exit_price:.6f}")
        logging.info(f"  사유: {reason}")
        logging.info(f"  가상 수익률: {pnl_pct:+.2f}% (실제 거래 없음)")
        logging.info(f"  → 다음 신호부터 실전 모드 전환")
        logging.info("━" * 60)
        
        # 텔레그램 알림
        name = self.config.get('name', self.symbol)
        emoji = "✅" if pnl_pct > 0 else "❌"
        msg = f"{emoji} <b>동기화 완료</b>\n\n"
        msg += f"코인: {name}\n"
        msg += f"방향: {self.side.upper()}\n"
        msg += f"진입: {self.entry_price:.4f}\n"
        msg += f"청산: {exit_price:.4f}\n"
        msg += f"사유: {reason}\n"
        msg += f"가상 수익률: {pnl_pct:+.2f}%\n\n"
        msg += f"→ 다음 신호부터 실전 진입!"
        telegram.send_sync(msg)
        
        # 시트 상태 업데이트
        self.sheets_manager.update_status(self.symbol, 'WAITING')
        
        # 상태 초기화
        self.is_active = False
        self.side = None
        self.entry_price = 0
        self.trailing_active = False

# ==============================================================================
# [실제 포지션 관리]
# ==============================================================================
class RealPositionManager:
    
    def __init__(self, exchange, symbol, config):
        self.exchange = exchange
        self.symbol = symbol
        self.config = config
        
        self.side = None
        self.entry_price = 0
        self.current_sl = 0
        self.qty = 0
        self.trailing_active = False
        self.extreme_price = 0
        self.is_active = False
        self._last_log_time = 0
        
    def open_position(self, side, entry_price):
        try:
            capital = self.config.get('capital', 30.0)
            leverage = self.config.get('leverage', 1)
            
            price_prec, amount_prec = get_precision(self.exchange, self.symbol)
            
            if entry_price is None or entry_price <= 0:
                logging.error(f"[{self.symbol}] 잘못된 진입가: {entry_price}")
                return False
            
            qty_raw = (capital * leverage) / float(entry_price)
            qty = truncate(qty_raw, amount_prec)
            
            if qty <= 0:
                logging.error(f"[{self.symbol}] 수량 계산 오류: {qty}")
                return False
            
            logging.info(f"[{self.symbol}] 주문 준비: {side.upper()} qty={qty} @ {entry_price}")
            
            try:
                self.exchange.set_leverage(leverage, self.symbol)
            except Exception as e:
                logging.warning(f"[{self.symbol}] 레버리지 설정 실패 (무시): {e}")
            
            order_side = 'buy' if side == 'long' else 'sell'
            order = self.exchange.create_market_order(self.symbol, order_side, qty)
            
            actual_entry = order.get('average')
            if actual_entry is None:
                time.sleep(0.5)
                ticker = self.exchange.fetch_ticker(self.symbol)
                actual_entry = ticker.get('last', entry_price)
            
            actual_entry = float(actual_entry)
            
            self.side = side
            self.entry_price = actual_entry
            self.qty = qty
            self.trailing_active = False
            
            sl_pct = self.config['stop_loss_pct'] / 100
            if side == 'long':
                self.current_sl = actual_entry * (1 - sl_pct)
                self.extreme_price = actual_entry
            else:
                self.current_sl = actual_entry * (1 + sl_pct)
                self.extreme_price = actual_entry
            
            self.is_active = True
            
            logging.info(f"[{self.symbol}] ✅ 포지션 오픈: {side.upper()} @ {actual_entry:.6f} SL:{self.current_sl:.6f}")
            
            # 텔레그램 알림
            name = self.config.get('name', self.symbol)
            emoji = "🟢" if side == 'long' else "🔴"
            msg = f"{emoji} <b>실전 진입</b>\n\n"
            msg += f"코인: {name}\n"
            msg += f"방향: {side.upper()}\n"
            msg += f"진입가: {actual_entry:.4f}\n"
            msg += f"수량: {qty}\n"
            msg += f"초기 SL: {self.current_sl:.4f}"
            telegram.send_sync(msg)
            
            return True
            
        except Exception as e:
            logging.error(f"[{self.symbol}] ❌ 포지션 오픈 실패: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return False
    
    def check_and_update(self, current_price):
        if not self.is_active:
            return False
        
        if current_price is None or current_price <= 0:
            return False
        
        now = time.time()
        if now - self._last_log_time > 30:
            status = "BE활성" if self.trailing_active else "대기"
            pnl = ((current_price / self.entry_price - 1) * 100) if self.side == 'long' else ((self.entry_price / current_price - 1) * 100)
            logging.info(f"[{self.symbol}] 현재가:{current_price:.4f} | PnL:{pnl:+.2f}% | SL:{self.current_sl:.4f} | {status}")
            self._last_log_time = now
        
        trigger_pct = self.config['trailing_trigger_pct'] / 100
        gap_pct = self.config['trailing_gap_pct'] / 100
        be_buffer = self.config['be_buffer_pct'] / 100
        
        should_close = False
        close_reason = ""
        
        if self.side == 'long':
            if not self.trailing_active:
                if current_price >= self.entry_price * (1 + trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 + be_buffer)
                    if self.current_sl < be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] Risk-Free 활성화! BE:{be_price:.6f}")
                    
                    # 텔레그램 알림
                    name = self.config.get('name', self.symbol)
                    msg = f"🛡️ <b>Risk-Free 활성화</b>\n\n"
                    msg += f"코인: {name}\n"
                    msg += f"BE가: {be_price:.4f}"
                    telegram.send_sync(msg)
            
            if self.trailing_active:
                if current_price > self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 - gap_pct)
                    if new_sl > self.current_sl:
                        self.current_sl = new_sl
            
            if current_price <= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        else:
            if not self.trailing_active:
                if current_price <= self.entry_price * (1 - trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 - be_buffer)
                    if self.current_sl > be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] Risk-Free 활성화! BE:{be_price:.6f}")
                    
                    # 텔레그램 알림
                    name = self.config.get('name', self.symbol)
                    msg = f"🛡️ <b>Risk-Free 활성화</b>\n\n"
                    msg += f"코인: {name}\n"
                    msg += f"BE가: {be_price:.4f}"
                    telegram.send_sync(msg)
            
            if self.trailing_active:
                if current_price < self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 + gap_pct)
                    if new_sl < self.current_sl:
                        self.current_sl = new_sl
            
            if current_price >= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        if should_close:
            self.close_position(close_reason, current_price)
        
        return should_close
    
    def close_position(self, reason, exit_price):
        try:
            close_side = 'sell' if self.side == 'long' else 'buy'
            
            self.exchange.create_market_order(
                self.symbol, 
                close_side, 
                self.qty,
                params={'reduceOnly': True}
            )
            
            if self.side == 'long':
                pnl_pct = (exit_price / self.entry_price - 1) * 100
            else:
                pnl_pct = (self.entry_price / exit_price - 1) * 100
            
            logging.info(f"[{self.symbol}] ✅ 포지션 청산: {reason} @ {exit_price:.6f} PnL:{pnl_pct:+.2f}%")
            
            # 텔레그램 알림
            name = self.config.get('name', self.symbol)
            emoji = "💰" if pnl_pct > 0 else "💸"
            result = "익절" if pnl_pct > 0 else "손절"
            msg = f"{emoji} <b>포지션 청산</b>\n\n"
            msg += f"코인: {name}\n"
            msg += f"방향: {self.side.upper()}\n"
            msg += f"진입: {self.entry_price:.4f}\n"
            msg += f"청산: {exit_price:.4f}\n"
            msg += f"사유: {reason}\n"
            msg += f"결과: {result} {pnl_pct:+.2f}%"
            telegram.send_sync(msg)
            
            self.is_active = False
            self.side = None
            self.entry_price = 0
            self.qty = 0
            self.trailing_active = False
            
            return True
            
        except Exception as e:
            logging.error(f"[{self.symbol}] ❌ 청산 실패: {e}")
            return False

# ==============================================================================
# [Bitget 웹소켓]
# ==============================================================================
class BitgetWebSocket:
    def __init__(self, on_candle_callback, on_price_callback):
        self.ws_url = "wss://ws.bitget.com/v2/ws/public"
        self.on_candle = on_candle_callback
        self.on_price = on_price_callback
        self.subscribed_symbols = set()
        self.ws = None
        self.is_connected = False
        
    async def connect(self):
        try:
            self.ws = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10
            )
            self.is_connected = True
            logging.info("Bitget 웹소켓 연결 성공!")
            return True
        except Exception as e:
            logging.error(f"웹소켓 연결 실패: {e}")
            return False
    
    async def subscribe(self, symbols, timeframe='3m'):
        if not self.ws:
            return False
        
        subscribe_args = []
        
        for symbol in symbols:
            clean_symbol = symbol.replace('/USDT:USDT', 'USDT')
            
            subscribe_args.append({
                "instType": "USDT-FUTURES",
                "channel": f"candle{timeframe}",
                "instId": clean_symbol
            })
            
            subscribe_args.append({
                "instType": "USDT-FUTURES",
                "channel": "ticker",
                "instId": clean_symbol
            })
            
            self.subscribed_symbols.add(symbol)
        
        subscribe_msg = {
            "op": "subscribe",
            "args": subscribe_args
        }
        
        await self.ws.send(json.dumps(subscribe_msg))
        logging.info(f"구독 요청: {len(symbols)}개 코인")
        
        return True
    
    async def listen(self):
        while self.is_connected:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30)
                data = json.loads(message)
                
                if 'event' in data:
                    if data['event'] == 'subscribe':
                        logging.info(f"구독 확인: {data.get('arg', {})}")
                    continue
                
                if 'data' in data and 'arg' in data:
                    channel = data['arg'].get('channel', '')
                    inst_id = data['arg'].get('instId', '')
                    
                    symbol = inst_id.replace('USDT', '/USDT:USDT')
                    
                    if channel.startswith('candle'):
                        for candle in data['data']:
                            self.on_candle(symbol, candle)
                    
                    elif channel == 'ticker':
                        for tick in data['data']:
                            price = float(tick.get('lastPr', 0))
                            if price > 0:
                                self.on_price(symbol, price)
                
            except asyncio.TimeoutError:
                try:
                    await self.ws.send('ping')
                except:
                    pass
            except websockets.ConnectionClosed:
                logging.warning("웹소켓 연결 끊김, 재연결 시도...")
                self.is_connected = False
                await asyncio.sleep(5)
                await self.connect()
                if self.subscribed_symbols:
                    await self.subscribe(list(self.subscribed_symbols))
            except Exception as e:
                logging.error(f"웹소켓 에러: {e}")
                await asyncio.sleep(1)
    
    async def close(self):
        self.is_connected = False
        if self.ws:
            await self.ws.close()

# ==============================================================================
# [메인 봇 클래스]
# ==============================================================================
class TradingBot:
    def __init__(self):
        self.exchange = None
        self.sheets_manager = GoogleSheetsManager()
        self.websocket = None
        
        self.virtual_managers = {}
        self.real_managers = {}
        
        self.last_signal_time = {}
        
    def initialize(self):
        self.exchange = ccxt.bitget({
            'apiKey': BITGET_API_KEY,
            'secret': BITGET_SECRET_KEY,
            'password': BITGET_PASSPHRASE,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        logging.info("마켓 정보 로딩...")
        self.exchange.load_markets()
        
        if not self.sheets_manager.connect():
            logging.error("Google Sheets 연결 실패!")
            return False
        
        configs = self.sheets_manager.get_coin_configs()
        if not configs:
            logging.error("활성화된 코인이 없습니다!")
            return False
        
        for symbol, config in configs.items():
            self._load_initial_candles(symbol, config)
            
            self.real_managers[symbol] = RealPositionManager(self.exchange, symbol, config)
            self.virtual_managers[symbol] = VirtualPositionManager(symbol, config, self.sheets_manager)
            
            status = config.get('status', 'WAITING')
            sync_entry = config.get('sync_entry_price', 0)
            
            if status == 'SYNC_LONG' and sync_entry > 0:
                self.virtual_managers[symbol].start_sync('long', sync_entry)
            elif status == 'SYNC_SHORT' and sync_entry > 0:
                self.virtual_managers[symbol].start_sync('short', sync_entry)
        
        logging.info(f"초기화 완료! {len(configs)}개 코인 준비됨")
        
        # 시작 알림
        msg = f"🚀 <b>봇 시작</b>\n\n"
        msg += f"버전: v24.3\n"
        msg += f"코인: {len(configs)}개 활성화"
        telegram.send_sync(msg)
        
        return True
    
    def _load_initial_candles(self, symbol, config):
    global candle_data
    
    try:
        timeframe = config.get('timeframe', '3m')
        
        # 3000개 이상 로드 (여러 번 호출)
        all_candles = []
        since = None
        target_count = 3000  # 원하는 캔들 수
        
        while len(all_candles) < target_count:
            ohlcv = self.exchange.fetch_ohlcv(
                symbol, 
                timeframe, 
                since=since,
                limit=1000
            )
            
            if not ohlcv:
                break
            
            if since is None:
                # 첫 호출: 최신 1000개
                all_candles = ohlcv
                # 더 과거 데이터 가져오기 위해 since 설정
                since = ohlcv[0][0] - 1  # 가장 오래된 것보다 1ms 이전
            else:
                # 이전 데이터를 앞에 붙이기
                all_candles = ohlcv + all_candles
                since = ohlcv[0][0] - 1
            
            time.sleep(0.1)  # rate limit
        
        candle_data[symbol] = all_candles
        logging.info(f"[{symbol}] 초기 캔들 {len(all_candles)}개 로드")
        
    except Exception as e:
        logging.error(f"[{symbol}] 초기 캔들 로드 실패: {e}")
    
    def on_candle_update(self, symbol, candle_raw):
        global candle_data, coin_configs
        
        if symbol not in coin_configs:
            return
        
        try:
            ts = int(candle_raw[0])
            o = float(candle_raw[1])
            h = float(candle_raw[2])
            l = float(candle_raw[3])
            c = float(candle_raw[4])
            v = float(candle_raw[5]) if len(candle_raw) > 5 else 0
            
            candle = [ts, o, h, l, c, v]
            candles = candle_data.get(symbol, [])
            
            if candles and candles[-1][0] == ts:
                candles[-1] = candle
            else:
                candles.append(candle)
                
                if len(candles) > 1000:
                    candles = candles[-1000:]
                
                candle_data[symbol] = candles
                
                self._check_signal(symbol)
                
        except Exception as e:
            logging.error(f"[{symbol}] 캔들 처리 에러: {e}")
    
    def on_price_update(self, symbol, price):
        if symbol in self.virtual_managers:
            vm = self.virtual_managers[symbol]
            if vm.is_active:
                closed, reason = vm.check_and_update(price)
                if closed:
                    if symbol in coin_configs:
                        coin_configs[symbol]['status'] = 'WAITING'
        
        if symbol in self.real_managers:
            rm = self.real_managers[symbol]
            if rm.is_active:
                rm.check_and_update(price)
    
    def _check_signal(self, symbol):
        global coin_configs
        
        config = coin_configs.get(symbol)
        if not config:
            return
        
        vm = self.virtual_managers.get(symbol)
        if vm and vm.is_active:
            return
        
        rm = self.real_managers.get(symbol)
        if not rm:
            return
        if rm.is_active:
            return
        
        status = config.get('status', 'WAITING')
        if status != 'WAITING':
            return
        
        now = datetime.now()
        last_time = self.last_signal_time.get(symbol)
        if last_time and (now - last_time).total_seconds() < 180:
            return
        
        signal, entry_price = generate_signal(symbol, config)
        
        if signal:
            logging.info(f"[{symbol}] 진입 시도: {signal.upper()} @ {entry_price}")
            
            success = rm.open_position(signal, entry_price)
            
            if success:
                self.last_signal_time[symbol] = now
    
    async def run(self):
        global coin_configs, is_running
        
        self.websocket = BitgetWebSocket(
            on_candle_callback=self.on_candle_update,
            on_price_callback=self.on_price_update
        )
        
        if not await self.websocket.connect():
            logging.error("웹소켓 연결 실패!")
            return
        
        symbols = list(coin_configs.keys())
        await self.websocket.subscribe(symbols)
        
        async def refresh_configs():
            while is_running:
                await asyncio.sleep(60)
                try:
                    old_configs = coin_configs.copy()
                    new_configs = self.sheets_manager.get_coin_configs(force_refresh=True)
                    
                    for symbol, config in new_configs.items():
                        if symbol not in self.real_managers:
                            self._load_initial_candles(symbol, config)
                            self.real_managers[symbol] = RealPositionManager(
                                self.exchange, symbol, config
                            )
                            self.virtual_managers[symbol] = VirtualPositionManager(
                                symbol, config, self.sheets_manager
                            )
                            await self.websocket.subscribe([symbol])
                            logging.info(f"새 코인 추가: {symbol}")
                        
                        # SYNC 상태인데 가상 포지션이 안 켜져있으면 시작
                        new_status = config.get('status', 'WAITING')
                        sync_entry = config.get('sync_entry_price', 0)
                        
                        if new_status in ['SYNC_LONG', 'SYNC_SHORT']:
                            vm = self.virtual_managers.get(symbol)
                            if vm and not vm.is_active and sync_entry > 0:
                                side = 'long' if new_status == 'SYNC_LONG' else 'short'
                                self.virtual_managers[symbol].start_sync(side, sync_entry)
                        
                        if symbol in self.real_managers:
                            self.real_managers[symbol].config = config
                        if symbol in self.virtual_managers:
                            self.virtual_managers[symbol].config = config
                            
                except Exception as e:
                    logging.error(f"설정 갱신 에러: {e}")
        
        async def log_status():
            while is_running:
                await asyncio.sleep(300)
                virtual_active = sum(1 for vm in self.virtual_managers.values() if vm.is_active)
                real_active = sum(1 for rm in self.real_managers.values() if rm.is_active)
                total = len(coin_configs)
                logging.info(f"상태: {total}개 코인 | 동기화중:{virtual_active} | 실전포지션:{real_active}")
        
        await asyncio.gather(
            self.websocket.listen(),
            refresh_configs(),
            log_status()
        )

# ==============================================================================
# [실행]
# ==============================================================================
def main():
    logging.info("=" * 70)
    logging.info("  Bitget Bot v24.3 - 웹소켓 + Google Sheets + 텔레그램")
    logging.info("=" * 70)
    
    bot = TradingBot()
    
    if not bot.initialize():
        logging.error("봇 초기화 실패!")
        sys.exit(1)
    
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logging.info("봇 종료...")
    except Exception as e:
        logging.error(f"치명적 에러: {e}")
        import traceback
        logging.error(traceback.format_exc())
        sys.exit(1)

if __name__ == '__main__':
    main()
