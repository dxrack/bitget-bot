"""
Bitget Bot v24 - 웹소켓 + Google Sheets 연동 버전
- 실시간 캔들 데이터 수신 (웹소켓)
- Google Sheets에서 코인별 설정 읽기
- 최대 20개 코인 동시 운영
- Supertrend + Engulfing 패턴 전략
- Risk-Free Trailing Stop
"""

import ccxt
import asyncio
import websockets
import json
import logging
import os
import sys
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np
import threading
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

# 텔레그램 (선택사항)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
    logging.error("ERROR: Missing Bitget API credentials!")
    sys.exit(1)

# ==============================================================================
# [전역 변수]
# ==============================================================================
# 코인별 데이터 저장
candle_data = defaultdict(list)  # {symbol: [candles]}
position_data = {}  # {symbol: position_info}
coin_configs = {}  # {symbol: config_from_sheets}

# 봇 상태
is_running = True
exchange = None

# ==============================================================================
# [Google Sheets 연동]
# ==============================================================================
class GoogleSheetsManager:
    def __init__(self):
        self.client = None
        self.sheet = None
        self.last_fetch_time = None
        self.cache_duration = 60  # 60초마다 갱신
        
    def connect(self):
        """Google Sheets 연결"""
        try:
            if GOOGLE_CREDENTIALS:
                # 환경변수에서 JSON 읽기
                creds_dict = json.loads(GOOGLE_CREDENTIALS)
            else:
                # 로컬 파일에서 읽기 (테스트용)
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
        """시트에서 코인 설정 읽기"""
        global coin_configs
        
        now = datetime.now()
        
        # 캐시된 데이터 사용
        if not force_refresh and self.last_fetch_time:
            if (now - self.last_fetch_time).total_seconds() < self.cache_duration:
                return coin_configs
        
        try:
            # 모든 데이터 읽기
            all_data = self.sheet.get_all_records()
            
            new_configs = {}
            for row in all_data:
                # 활성화된 코인만
                if str(row.get('활성화', 'N')).upper() == 'Y':
                    symbol = row.get('거래쌍', '')
                    if symbol:
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
                            'capital': 30.0,  # 기본값
                            'leverage': 1
                        }
                        logging.info(f"코인 로드: {symbol} (활성화)")
            
            coin_configs = new_configs
            self.last_fetch_time = now
            
            logging.info(f"총 {len(coin_configs)}개 코인 활성화됨")
            return coin_configs
            
        except Exception as e:
            logging.error(f"시트 읽기 실패: {e}")
            return coin_configs
    
    def update_position_status(self, symbol, status, entry_price=None, current_sl=None):
        """시트에 포지션 상태 업데이트 (선택사항)"""
        # 필요시 구현
        pass

# ==============================================================================
# [Supertrend 계산]
# ==============================================================================
def calculate_supertrend(candles, atr_period, multiplier):
    """
    Supertrend 계산
    candles: [(timestamp, open, high, low, close, volume), ...]
    """
    if len(candles) < atr_period + 10:
        return None, None
    
    # numpy 배열로 변환
    data = np.array(candles, dtype=np.float64)
    high = data[:, 2]
    low = data[:, 3]
    close = data[:, 4]
    n = len(close)
    
    # TR 계산
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    
    # ATR (EMA 방식)
    atr = np.zeros(n)
    atr[0] = tr[0]
    alpha = 1.0 / atr_period
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    
    # Supertrend 계산
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
    """
    Engulfing 패턴 감지
    Returns: 'bullish', 'bearish', or None
    """
    if len(candles) < 3:
        return None
    
    # 마지막 완성된 캔들 기준 ([-2]가 신호 캔들, [-1]은 현재 진행 중)
    prev_candle = candles[-3]  # 이전 캔들
    signal_candle = candles[-2]  # 신호 캔들
    
    prev_open = prev_candle[1]
    prev_close = prev_candle[4]
    curr_open = signal_candle[1]
    curr_close = signal_candle[4]
    
    # Bullish Engulfing
    is_bullish = (
        prev_close < prev_open and  # 이전 음봉
        curr_close > curr_open and  # 현재 양봉
        curr_close > prev_open and  # 현재 종가 > 이전 시가
        curr_open <= prev_close     # 현재 시가 <= 이전 종가
    )
    
    # Bearish Engulfing
    is_bearish = (
        prev_close > prev_open and  # 이전 양봉
        curr_close < curr_open and  # 현재 음봉
        curr_open >= prev_close and # 현재 시가 >= 이전 종가
        curr_close < prev_open      # 현재 종가 < 이전 시가
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
    """
    특정 코인의 신호 생성
    """
    global candle_data
    
    candles = candle_data.get(symbol, [])
    
    if len(candles) < 100:
        return None, None
    
    # Supertrend 계산
    current_trend, prev_trend = calculate_supertrend(
        candles, 
        config['atr_period'], 
        config['atr_multiplier']
    )
    
    if current_trend is None:
        return None, None
    
    # 패턴 감지
    pattern = detect_engulfing(candles)
    
    if pattern is None:
        return None, None
    
    # 다음 캔들 시가 (현재 캔들)
    entry_price = candles[-1][1]  # 현재 캔들 시가
    
    # 신호 판단
    # LONG: 상승 트렌드 + Bullish Engulfing
    if current_trend == 1 and pattern == 'bullish':
        logging.info(f"[{symbol}] LONG 신호! 트렌드:상승 패턴:Bullish @ {entry_price}")
        return 'long', entry_price
    
    # SHORT: 하락 트렌드 + Bearish Engulfing
    if current_trend == -1 and pattern == 'bearish':
        logging.info(f"[{symbol}] SHORT 신호! 트렌드:하락 패턴:Bearish @ {entry_price}")
        return 'short', entry_price
    
    return None, None

# ==============================================================================
# [포지션 관리 클래스]
# ==============================================================================
class PositionManager:
    def __init__(self, exchange, symbol, config):
        self.exchange = exchange
        self.symbol = symbol
        self.config = config
        
        self.side = None  # 'long' or 'short'
        self.entry_price = 0
        self.current_sl = 0
        self.qty = 0
        self.trailing_active = False
        self.extreme_price = 0
        
        self.is_active = False
        
    def open_position(self, side, entry_price):
        """포지션 진입"""
        try:
            capital = self.config.get('capital', 30.0)
            leverage = self.config.get('leverage', 1)
            
            # 수량 계산
            qty = (capital * leverage) / entry_price
            
            # 정밀도 조정
            market = self.exchange.market(self.symbol)
            amount_precision = market['precision']['amount']
            if amount_precision and amount_precision > 0:
                qty = int(qty * (10 ** amount_precision)) / (10 ** amount_precision)
            
            # 레버리지 설정
            self.exchange.set_leverage(leverage, self.symbol)
            
            # 주문 실행
            order_side = 'buy' if side == 'long' else 'sell'
            order = self.exchange.create_market_order(self.symbol, order_side, qty)
            
            # 실제 체결가
            actual_entry = float(order.get('average', entry_price))
            
            # 포지션 정보 저장
            self.side = side
            self.entry_price = actual_entry
            self.qty = qty
            self.trailing_active = False
            
            # 초기 SL 설정
            sl_pct = self.config['stop_loss_pct'] / 100
            if side == 'long':
                self.current_sl = actual_entry * (1 - sl_pct)
                self.extreme_price = actual_entry
            else:
                self.current_sl = actual_entry * (1 + sl_pct)
                self.extreme_price = actual_entry
            
            self.is_active = True
            
            logging.info(f"[{self.symbol}] 포지션 오픈: {side.upper()} @ {actual_entry:.6f} SL:{self.current_sl:.6f}")
            
            return True
            
        except Exception as e:
            logging.error(f"[{self.symbol}] 포지션 오픈 실패: {e}")
            return False
    
    def check_and_update(self, current_price):
        """가격 업데이트 및 청산 체크"""
        if not self.is_active:
            return False
        
        trigger_pct = self.config['trailing_trigger_pct'] / 100
        gap_pct = self.config['trailing_gap_pct'] / 100
        be_buffer = self.config['be_buffer_pct'] / 100
        
        should_close = False
        close_reason = ""
        
        if self.side == 'long':
            # 트레일링 트리거 체크
            if not self.trailing_active:
                if current_price >= self.entry_price * (1 + trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 + be_buffer)
                    if self.current_sl < be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] Risk-Free 활성화! BE:{be_price:.6f}")
            
            # 트레일링 SL 업데이트
            if self.trailing_active:
                if current_price > self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 - gap_pct)
                    if new_sl > self.current_sl:
                        self.current_sl = new_sl
            
            # 청산 체크
            if current_price <= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        else:  # short
            # 트레일링 트리거 체크
            if not self.trailing_active:
                if current_price <= self.entry_price * (1 - trigger_pct):
                    self.trailing_active = True
                    be_price = self.entry_price * (1 - be_buffer)
                    if self.current_sl > be_price:
                        self.current_sl = be_price
                    self.extreme_price = current_price
                    logging.info(f"[{self.symbol}] Risk-Free 활성화! BE:{be_price:.6f}")
            
            # 트레일링 SL 업데이트
            if self.trailing_active:
                if current_price < self.extreme_price:
                    self.extreme_price = current_price
                    new_sl = self.extreme_price * (1 + gap_pct)
                    if new_sl < self.current_sl:
                        self.current_sl = new_sl
            
            # 청산 체크
            if current_price >= self.current_sl:
                should_close = True
                close_reason = "Risk-Free" if self.trailing_active else "SL"
        
        if should_close:
            self.close_position(close_reason, current_price)
        
        return should_close
    
    def close_position(self, reason, exit_price):
        """포지션 청산"""
        try:
            close_side = 'sell' if self.side == 'long' else 'buy'
            
            self.exchange.create_market_order(
                self.symbol, 
                close_side, 
                self.qty,
                params={'reduceOnly': True}
            )
            
            # PnL 계산
            if self.side == 'long':
                pnl_pct = (exit_price / self.entry_price - 1) * 100
            else:
                pnl_pct = (self.entry_price / exit_price - 1) * 100
            
            logging.info(f"[{self.symbol}] 포지션 청산: {reason} @ {exit_price:.6f} PnL:{pnl_pct:+.2f}%")
            
            # 상태 초기화
            self.is_active = False
            self.side = None
            self.entry_price = 0
            self.qty = 0
            self.trailing_active = False
            
            return True
            
        except Exception as e:
            logging.error(f"[{self.symbol}] 청산 실패: {e}")
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
        """웹소켓 연결"""
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
        """캔들 구독"""
        if not self.ws:
            return False
        
        subscribe_args = []
        
        for symbol in symbols:
            # LINK/USDT:USDT -> LINKUSDT
            clean_symbol = symbol.replace('/USDT:USDT', 'USDT')
            
            # 캔들 구독
            subscribe_args.append({
                "instType": "USDT-FUTURES",
                "channel": f"candle{timeframe}",
                "instId": clean_symbol
            })
            
            # 틱 가격 구독 (포지션 모니터링용)
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
        """메시지 수신 루프"""
        while self.is_connected:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=30)
                data = json.loads(message)
                
                # 응답 처리
                if 'event' in data:
                    if data['event'] == 'subscribe':
                        logging.info(f"구독 확인: {data.get('arg', {})}")
                    continue
                
                # 데이터 처리
                if 'data' in data and 'arg' in data:
                    channel = data['arg'].get('channel', '')
                    inst_id = data['arg'].get('instId', '')
                    
                    # LINKUSDT -> LINK/USDT:USDT
                    symbol = inst_id.replace('USDT', '/USDT:USDT')
                    
                    if channel.startswith('candle'):
                        # 캔들 데이터
                        for candle in data['data']:
                            self.on_candle(symbol, candle)
                    
                    elif channel == 'ticker':
                        # 틱 가격
                        for tick in data['data']:
                            price = float(tick.get('lastPr', 0))
                            if price > 0:
                                self.on_price(symbol, price)
                
            except asyncio.TimeoutError:
                # 연결 유지를 위한 ping
                try:
                    await self.ws.send('ping')
                except:
                    pass
            except websockets.ConnectionClosed:
                logging.warning("웹소켓 연결 끊김, 재연결 시도...")
                self.is_connected = False
                await asyncio.sleep(5)
                await self.connect()
                # 재구독
                if self.subscribed_symbols:
                    await self.subscribe(list(self.subscribed_symbols))
            except Exception as e:
                logging.error(f"웹소켓 에러: {e}")
                await asyncio.sleep(1)
    
    async def close(self):
        """연결 종료"""
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
        self.position_managers = {}  # {symbol: PositionManager}
        self.last_signal_time = {}   # {symbol: datetime}
        
    def initialize(self):
        """초기화"""
        # Exchange 연결
        self.exchange = ccxt.bitget({
            'apiKey': BITGET_API_KEY,
            'secret': BITGET_SECRET_KEY,
            'password': BITGET_PASSPHRASE,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        # Google Sheets 연결
        if not self.sheets_manager.connect():
            logging.error("Google Sheets 연결 실패!")
            return False
        
        # 코인 설정 로드
        configs = self.sheets_manager.get_coin_configs()
        if not configs:
            logging.error("활성화된 코인이 없습니다!")
            return False
        
        # 초기 캔들 데이터 로드
        for symbol, config in configs.items():
            self._load_initial_candles(symbol, config)
            self.position_managers[symbol] = PositionManager(self.exchange, symbol, config)
        
        logging.info(f"초기화 완료! {len(configs)}개 코인 준비됨")
        return True
    
    def _load_initial_candles(self, symbol, config):
        """초기 캔들 데이터 로드 (Supertrend 계산용)"""
        global candle_data
        
        try:
            timeframe = config.get('timeframe', '3m')
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=500)
            
            # [(timestamp, open, high, low, close, volume), ...]
            candle_data[symbol] = ohlcv
            logging.info(f"[{symbol}] 초기 캔들 {len(ohlcv)}개 로드")
            
        except Exception as e:
            logging.error(f"[{symbol}] 초기 캔들 로드 실패: {e}")
    
    def on_candle_update(self, symbol, candle_raw):
        """새 캔들 수신 콜백"""
        global candle_data, coin_configs
        
        if symbol not in coin_configs:
            return
        
        try:
            # Bitget 웹소켓 캔들 포맷: [timestamp, open, high, low, close, volume, ...]
            ts = int(candle_raw[0])
            o = float(candle_raw[1])
            h = float(candle_raw[2])
            l = float(candle_raw[3])
            c = float(candle_raw[4])
            v = float(candle_raw[5]) if len(candle_raw) > 5 else 0
            
            candle = [ts, o, h, l, c, v]
            
            # 기존 캔들 업데이트 또는 추가
            candles = candle_data.get(symbol, [])
            
            if candles and candles[-1][0] == ts:
                # 같은 타임스탬프면 업데이트
                candles[-1] = candle
            else:
                # 새 캔들이면 추가
                candles.append(candle)
                
                # 최대 1000개 유지
                if len(candles) > 1000:
                    candles = candles[-1000:]
                
                candle_data[symbol] = candles
                
                # 새 캔들 완성 = 신호 체크
                self._check_signal(symbol)
                
        except Exception as e:
            logging.error(f"[{symbol}] 캔들 처리 에러: {e}")
    
    def on_price_update(self, symbol, price):
        """가격 업데이트 콜백 (포지션 모니터링)"""
        if symbol not in self.position_managers:
            return
        
        pm = self.position_managers[symbol]
        if pm.is_active:
            pm.check_and_update(price)
    
    def _check_signal(self, symbol):
        """신호 체크 및 진입"""
        global coin_configs
        
        config = coin_configs.get(symbol)
        if not config:
            return
        
        pm = self.position_managers.get(symbol)
        if not pm:
            return
        
        # 이미 포지션이 있으면 스킵
        if pm.is_active:
            return
        
        # 쿨다운 체크 (같은 코인 연속 진입 방지, 3분)
        now = datetime.now()
        last_time = self.last_signal_time.get(symbol)
        if last_time and (now - last_time).total_seconds() < 180:
            return
        
        # 신호 생성
        signal, entry_price = generate_signal(symbol, config)
        
        if signal:
            logging.info(f"[{symbol}] 진입 시도: {signal.upper()} @ {entry_price}")
            
            success = pm.open_position(signal, entry_price)
            
            if success:
                self.last_signal_time[symbol] = now
    
    async def run(self):
        """메인 실행"""
        global coin_configs
        
        # 웹소켓 설정
        self.websocket = BitgetWebSocket(
            on_candle_callback=self.on_candle_update,
            on_price_callback=self.on_price_update
        )
        
        # 연결
        if not await self.websocket.connect():
            logging.error("웹소켓 연결 실패!")
            return
        
        # 구독
        symbols = list(coin_configs.keys())
        await self.websocket.subscribe(symbols)
        
        # 설정 주기적 갱신 태스크
        async def refresh_configs():
            while is_running:
                await asyncio.sleep(60)
                try:
                    new_configs = self.sheets_manager.get_coin_configs(force_refresh=True)
                    
                    # 새로 추가된 코인 처리
                    for symbol in new_configs:
                        if symbol not in self.position_managers:
                            self._load_initial_candles(symbol, new_configs[symbol])
                            self.position_managers[symbol] = PositionManager(
                                self.exchange, symbol, new_configs[symbol]
                            )
                            await self.websocket.subscribe([symbol])
                            logging.info(f"새 코인 추가: {symbol}")
                except Exception as e:
                    logging.error(f"설정 갱신 에러: {e}")
        
        # 상태 로깅 태스크
        async def log_status():
            while is_running:
                await asyncio.sleep(300)  # 5분마다
                active = sum(1 for pm in self.position_managers.values() if pm.is_active)
                total = len(self.position_managers)
                logging.info(f"상태: {total}개 코인 모니터링 중 | 활성 포지션: {active}개")
        
        # 태스크 실행
        await asyncio.gather(
            self.websocket.listen(),
            refresh_configs(),
            log_status()
        )

# ==============================================================================
# [실행]
# ==============================================================================
def main():
    logging.info("="*70)
    logging.info("  Bitget Bot v24 - 웹소켓 + Google Sheets")
    logging.info("="*70)
    
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
        sys.exit(1)

if __name__ == '__main__':
    main()
