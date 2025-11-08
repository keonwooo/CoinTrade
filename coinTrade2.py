# coinTrade2.py — unified single-file bot (basic / volatility / volume / rsi / rsi_trend)

from dataclasses import dataclass
from typing import Tuple, Optional
import os, math, time, json, uuid, logging, threading, argparse, sys, subprocess
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import deque
import statistics
import requests
import discord_notify

import yaml
from dotenv import load_dotenv
import pyupbit
from wcwidth import wcswidth

# 외부 모듈 (동일)
from kakao_notify import send_kakao_text
from fg_index import FearGreedWorker
from price_source import PriceStream

# ======================
# 경로/상수 (기본값)
# ======================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR   = os.path.join(BASE_DIR, "config")
CONFIG_FILE  = os.path.join(CONFIG_DIR, "config.yml")
BANNER_FILE  = os.path.join(CONFIG_DIR, "banner.txt")
ENV_FILE     = os.path.join(CONFIG_DIR, ".env")

# 모드 확정 이전의 “임시” 기본경로 (main에서 모드 확정 후 재설정)
LOG_DIR      = os.path.join(BASE_DIR, "logs")
STATE_FILE   = os.path.join(LOG_DIR, "state.json")
PAPER_STATE  = os.path.join(LOG_DIR, "paper_state.json")

# ======================
# 전역 런타임 객체 (main에서 셋업)
# ======================
logger: logging.Logger = logging.getLogger("upbit-bot")
action_logger: logging.Logger = logging.getLogger("trade-action")
log = logger

state_lock = threading.Lock()
state = None            # main()에서 로드
sim_balance = None      # main()에서 로드

REST_COOLDOWN_SEC = 600.0
REST_FAIL_UNTIL = 0.0

STRAT_TAG = "[BASIC]"

# ======================
# Config 모델
# ======================
@dataclass
class Config:
    ticker: str
    seed_krw: float
    parts: int
    drop_pct: float
    rise_pct: float
    min_order_krw: float
    cooldown_sec: float
    max_buys_per_hour: int
    poll_sec: float
    log_every_sec: float
    paper_trade: bool
    log_dir: str
    ws_stale_fallback_sec: float
    cron_log: Optional[str] = None

def load_config(path: str = CONFIG_FILE) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    t = doc.get("trade", {}) or {}

    # upbit
    upbit_cfg = doc.get("upbit") or {}
    global REST_COOLDOWN_SEC
    # rest_cooldown_sec 둘 다 허용
    REST_COOLDOWN_SEC = float(
        upbit_cfg.get("rest_cooldown_sec", REST_COOLDOWN_SEC)
    )

    return Config(
        ticker=t.get("ticker", "KRW-BTC"),
        seed_krw=float(t.get("seed_krw", 1_000_000)),
        parts=int(t.get("parts", 40)),
        drop_pct=float(t.get("drop_pct", 0.01)),
        rise_pct=float(t.get("rise_pct", 0.03)),
        min_order_krw=float(t.get("min_order_krw", 5000)),
        cooldown_sec=float(t.get("cooldown_sec", 20.0)),
        max_buys_per_hour=int(t.get("max_buys_per_hour", 3)),
        poll_sec=float(t.get("poll_sec", 1.0)),
        log_every_sec=float(t.get("log_every_sec", 5.0)),
        cron_log=t.get("cron_log", None),
        paper_trade=bool(t.get("paper_trade", True)),
        log_dir=t.get("log_dir", LOG_DIR),
        ws_stale_fallback_sec=float(t.get("ws_stale_fallback_sec", 3.0)),
    )

def load_banner(path: str = BANNER_FILE) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""

KST = ZoneInfo("Asia/Seoul")
def now_kst() -> datetime:
    return datetime.now(tz=KST)

def next_kst_0900(after: Optional[datetime] = None) -> datetime:
    base = after.astimezone(KST) if after else now_kst()
    target_today = base.replace(hour=9, minute=0, second=0, microsecond=0)
    if base < target_today:
        return target_today
    return (target_today + timedelta(days=1))

# ======================
# 실행 인자
# ======================
def parse_args():
    p = argparse.ArgumentParser(description="Unified Upbit bot")
    p.add_argument(
        "--logs-root",
        default=None,
        help="로그 루트 디렉토리(지정 시 logs/<mode> 하위에 기록). 미지정이면 코드 위치(BASE_DIR) 기준"
    )
    p.add_argument(
        "--mode",
        choices=["basic", "volatility", "volume", "rsi", "rsi_trend"],
        default="basic",
        help="전략 선택 (basic | volatility | volume | rsi | rsi_trend)"
    )
    return p.parse_args()

def pick_log_dir_by_mode(mode: str) -> str:
    if mode == "volatility": return "volatility"
    if mode == "volume":     return "volume"
    if mode == "rsi":        return "rsi"
    if mode == "rsi_trend":  return "rsi_trend"
    return "basic"  # basic

def _abspath_under_base(logs_root: str, path: str) -> str:
    return os.path.abspath(os.path.join(logs_root, "logs", path))

# ======================
# 변동성/RSI/볼륨 헬퍼
# ======================
class VolatilityGuard:
    def __init__(self, window_sec: int = 300):
        self.window_sec = max(30, window_sec)
        self.buf = deque()
    def add(self, ts: float, price: float):
        self.buf.append((ts, price))
        while self.buf and ts - self.buf[0][0] > self.window_sec:
            self.buf.popleft()
    def realized_vol_pct(self) -> float:
        if len(self.buf) < 10:
            return 0.0
        rets = []
        prev = None
        for _, p in self.buf:
            if prev and p > 0 and prev > 0:
                rets.append((p/prev - 1.0) * 100.0)
            prev = p
        if len(rets) < 5:
            return 0.0
        return statistics.pstdev(rets)

class RSIBuffer:
    def __init__(self, period: int = 14, maxlen: int = 300):
        self.period = period
        self.buf = deque(maxlen=maxlen)
    def add(self, price: float):
        if price and price > 0:
            self.buf.append(price)
    def rsi(self) -> float:
        if len(self.buf) < self.period + 1:
            return 50.0
        gains = 0.0
        losses = 0.0
        for i in range(-self.period, 0):
            diff = self.buf[i] - self.buf[i-1]
            if diff > 0: gains += diff
            else:        losses -= diff
        if gains == 0 and losses == 0:
            return 50.0
        rs = (gains / self.period) / (losses / self.period if losses != 0 else 1e-9)
        return 100.0 - (100.0 / (1.0 + rs))

class EMACalc:
    """지수이동평균(EMA) 계산기. update(price)로 최신 ema 반환."""
    def __init__(self, period: int):
        self.period = max(1, int(period))
        self.alpha = 2.0 / (self.period + 1.0)
        self.ema: Optional[float] = None

    def seed(self, prices: list[float]):
        vals = [float(p) for p in prices if p and p > 0]
        if not vals:
            return
        sma = sum(vals) / len(vals)
        self.ema = sma
    
    def update(self, price: float) -> float:
        if price is None or price <= 0:
            return self.ema if self.ema is not None else 0.0
        if self.ema is None:
            self.ema = price
        else:
            self.ema = (price - self.ema) * self.alpha + self.ema
        return self.ema

class ReboundDetector:
    """
    반등 조건:
    - 최근 lookback 구간에서 가격이 EMA_short 아래로 내려간 적이 있었고
    - 지금은 가격 >= EMA_short 이며
    - 단기 추세 우상향(EMA_short > EMA_long)
    """
    def __init__(self, lookback_ticks: int = 5):
        self.look = max(1, int(lookback_ticks))
        self.was_below_short = deque(maxlen=self.look)
    
    def update_and_check(self, price: float, ema_s: float, ema_l: float) -> bool:
        below = (price > 0 and ema_s > 0 and price < ema_s)
        self.was_below_short.append(below)
        had_below = any(self.was_below_short)
        rebound_now = (price >= ema_s) and (ema_s > ema_l) and had_below
        return bool(rebound_now)

def get_volume_ratio(ticker: str, count: int = 30) -> float:
    """1분봉 최근 avg 대비 최신봉 거래량 비율. 실패 시 1.0"""
    global REST_FAIL_UNTIL
    now = time.time()
    if now < REST_FAIL_UNTIL:
        return 1.0
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute1", count=count)
        if df is None or len(df) < 5:
            return 1.0
        vols = df["volume"].values
        if len(vols) < 2:
            return 1.0
        avg = float(vols[:-1].mean()) if hasattr(vols[:-1], "mean") else (sum(vols[:-1]) / max(1, len(vols[:-1])))
        cur = float(vols[-1])
        if avg <= 0:
            return 1.0
        return cur / avg
    except Exception as e:
        log.warning(f"[REST] 거래량 조회 실패: {e}")
        REST_FAIL_UNTIL = now + REST_COOLDOWN_SEC
        return 1.0

# ======================
# 공통 유틸/주문/상태
# ======================
def fmt_krw(v: float) -> str:
    try:
        return f"{float(v):,.0f} KRW"
    except Exception:
        return "-"

def fmt_pct(price: float, avg: float) -> str:
    if avg and avg > 0:
        return f"{(price/avg - 1)*100:+.2f}%"
    return "-"

def is_cron_match(cron_expr: str, dt: datetime) -> bool:
    """
    단순 크론식 매칭: "0 0 * * * *" (초 분 시 일 월 요일)
    매칭되면 True 반환.
    """
    parts = cron_expr.strip().split()
    if len(parts) != 6:
        return False
    
    sec, minute, hour, day, month, weekday = parts

    def match(field, value):
        if field == "*":
            return True
        try:
            return int(field) == value
        except ValueError:
            return False
    
    return (
        match(sec, dt.second)
        and match(minute, dt.minute)
        and match(hour, dt.hour)
        and match(day, dt.day)
        and match(month, dt.month)
        and match(weekday, dt.weekday())
    )

def get_rest_price(ticker: str) -> float:
    global REST_FAIL_UNTIL
    now = time.time()
    if now < REST_FAIL_UNTIL:
        return 0.0
    try:
        v = pyupbit.get_current_price(ticker)
        return float(v or 0)
    except Exception as e:
        log.warning(f"[REST] 현재가 조회 실패: {e}")
        REST_FAIL_UNTIL = now + REST_COOLDOWN_SEC
        return 0.0

def new_op_code(cfg) -> str:
    base = cfg.ticker.replace("-", "")
    return f"{base}-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def pad_label(text: str, width: int) -> str:
    w = wcswidth(text)
    if w < 0: w = len(text)
    return text + " " * max(0, width - w)

def is_valid_price(p: float) -> bool:
    return isinstance(p, (int, float)) and p >= 1.0

# 시간당 매수 제한
BUY_HISTORY = []
def can_buy_now(cfg) -> bool:
    global BUY_HISTORY
    now = time.time()
    BUY_HISTORY = [t for t in BUY_HISTORY if now - t <= 3600]
    return len(BUY_HISTORY) < cfg.max_buys_per_hour
def record_buy_time():
    BUY_HISTORY.append(time.time())

# 잔고/주문 (CFG/state/sim_balance/action_logger 사용)
def get_krw_and_coin_balance(cfg, upbit) -> Tuple[float, float, float]:
    global REST_FAIL_UNTIL, sim_balance
    if cfg.paper_trade:
        return sim_balance["KRW"], sim_balance["coin"], sim_balance["avg"]
    else:
        now = time.time()
        if now < REST_FAIL_UNTIL:
            return 0.0, 0.0, 0.0
        try:
            balances = upbit.get_balances()
        except Exception as e:
            log.warning(f"[REST] 잔고 조회 실패: {e}")
            REST_FAIL_UNTIL = now + REST_COOLDOWN_SEC
            return 0.0, 0.0, 0.0
        krw = coin_bal = avg = 0.0
        base = cfg.ticker.split("-")[1]
        for b in balances:
            if b.get("currency") == "KRW":
                krw = float(b.get("balance", 0) or 0)
            if b.get("currency") == base:
                coin_bal = float(b.get("balance", 0) or 0)
                avg = float(b.get("avg_buy_price", 0) or 0)
        return krw, coin_bal, avg

def buy_unit_krw_with_available(cfg, upbit, unit_krw: float, available_krw: float, 
                                price: float, mode: str, doc: dict) -> bool:
    global sim_balance, state
    if not is_valid_price(price):
        log.warning("[TRADE] 매수 스킵: 유효하지 않은 가격 값 (price<=0)")
        return False
    amt = math.floor(min(unit_krw, available_krw))
    if amt < cfg.min_order_krw:
        log.info(f"매수 스킵: 가용 KRW {available_krw:,.0f} < 최소주문액 {cfg.min_order_krw:,.0f}")
        return False

    if cfg.paper_trade:
        volume = amt / price
        prev_coin = sim_balance["coin"]
        prev_cost = sim_balance["avg"] * prev_coin
        new_coin = prev_coin + volume
        sim_balance["coin"] = new_coin
        sim_balance["KRW"] -= amt
        sim_balance["avg"] = (prev_cost + amt) / new_coin if new_coin > 0 else 0
        with state_lock:
            if not state["in_position"]:
                state["op_code"] = new_op_code(cfg)
                state["in_position"] = True
                state["ladder_idx"] = 0
            state["entry_avg"] = sim_balance["avg"]
            state["last_action"] = "BUY"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        write_json(PAPER_STATE, sim_balance)
        msg = (f"{STRAT_TAG} [{state['op_code']}] [모의] 매수 {amt:,.0f} KRW "
               f"→ 평단={sim_balance['avg']:,.0f}, "
               f"등락률={fmt_pct(price, sim_balance['avg'])}")
        log.info(msg); action_logger.info(msg)
        notify(mode, msg, doc)
        return True
    else:
        r = upbit.buy_market_order(cfg.ticker, amt)
        ok = bool(r and r.get("uuid"))
        _, _, avg_after = get_krw_and_coin_balance(cfg, upbit)
        with state_lock:
            if ok and not state["in_position"]:
                state["op_code"] = new_op_code(cfg)
                state["in_position"] = True
                state["ladder_idx"] = 0
            if ok:
                state["entry_avg"] = avg_after
            state["last_action"] = "BUY" if ok else "BUY_FAIL"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        msg = f"{STRAT_TAG} [{state['op_code'] or '-'}] 시장가 매수 {amt:,.0f} KRW → {'성공' if ok else '실패'} ({r})"
        log.info(msg); action_logger.info(msg)
        notify(mode, msg, doc)
        return ok

def sell_all_market(cfg, upbit, volume: float, price: float, mode: str, doc: dict) -> bool:
    global sim_balance, state
    if not is_valid_price(price):
        log.warning("[TRADE] 매도 스킵: 유효하지 않은 가격 (price<=0)")
        return False
    if volume <= 0:
        log.info("매도 스킵: 보유수량 0")
        return False

    if cfg.paper_trade:
        krw_gain = volume * price
        prev_avg = sim_balance["avg"]
        with state_lock:
            op = state["op_code"] or new_op_code(cfg)
        sim_balance["KRW"] += krw_gain
        sim_balance["coin"] = 0.0
        sim_balance["avg"] = 0.0
        with state_lock:
            state["in_position"] = False
            state["last_action"] = "SELL"
            state["last_action_ts"] = time.time()
            closed_op = state["op_code"]
            state["op_code"] = ""
            state["ladder_idx"] = 0
            write_json(STATE_FILE, state)
        write_json(PAPER_STATE, sim_balance)
        msg = (f"{STRAT_TAG} [{closed_op or op}] [모의] 전량매도 {volume:.8f} coin @ {price:,.0f} → "
               f"잔고={sim_balance['KRW']:,.0f} (직전 등락률 {fmt_pct(price, prev_avg)})")
        log.info(msg); action_logger.info(msg)
        notify(mode, msg, doc)
        return True
    else:
        r = upbit.sell_market_order(cfg.ticker, volume)
        ok = bool(r and r.get("uuid"))
        with state_lock:
            op = state["op_code"] or new_op_code(cfg)
            if ok:
                state["in_position"] = False
                closed_op = state["op_code"]
                state["op_code"] = ""
                state["ladder_idx"] = 0
            else:
                closed_op = state["op_code"]
            state["last_action"] = "SELL" if ok else "SELL_FAIL"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        msg = f"{STRAT_TAG} [{closed_op or op}] 시장가 전량매도 {volume:.8f} → {'성공' if ok else '실패'} ({r})"
        log.info(msg); action_logger.info(msg)
        notify(mode, msg, doc)
        return ok

def calc_unit_krw_by_initial_balance(cfg, upbit) -> float:
    krw, _, _ = get_krw_and_coin_balance(cfg, upbit)
    base = cfg.seed_krw if cfg.paper_trade else max(krw, cfg.min_order_krw)
    return max(cfg.min_order_krw, math.floor(base / cfg.parts))

# 설정 자동 리로드
_cfg_lock = threading.Lock()
_cfg_mtime = None

def apply_runtime_safe_updates(old: Config, new: Config, logger_ref: logging.Logger):
    updated = []
    with _cfg_lock:
        if new.drop_pct != old.drop_pct:
            old.drop_pct = new.drop_pct; updated.append("drop_pct")
        if new.rise_pct != old.rise_pct:
            old.rise_pct = new.rise_pct; updated.append("rise_pct")
        if new.parts != old.parts:
            old.parts = new.parts; updated.append("parts")
        if new.min_order_krw != old.min_order_krw:
            old.min_order_krw = new.min_order_krw; updated.append("min_order_krw")
        if new.cooldown_sec != old.cooldown_sec:
            old.cooldown_sec = new.cooldown_sec; updated.append("cooldown_sec")
        if new.max_buys_per_hour != old.max_buys_per_hour:
            old.max_buys_per_hour = new.max_buys_per_hour; updated.append("max_buys_per_hour")
        if new.log_every_sec != old.log_every_sec:
            old.log_every_sec = new.log_every_sec; updated.append("log_every_sec")
        if new.ws_stale_fallback_sec != old.ws_stale_fallback_sec:
            old.ws_stale_fallback_sec = new.ws_stale_fallback_sec; updated.append("ws_stale_fallback_sec")
        for k in ["paper_trade","seed_krw","ticker","log_dir","poll_sec"]:
            if getattr(new, k) != getattr(old, k):
                logger_ref.warning(f"{k}는 실행 중 변경 비권장 → 무시")
    if updated:
        logger_ref.info(f"⚡ config.yml 변경 감지 → 적용됨: {', '.join(updated)}")

def maybe_reload_config(cfg_ref: Config, logger_ref: logging.Logger, path: str = CONFIG_FILE):
    global _cfg_mtime
    try:
        m = os.path.getmtime(path)
    except FileNotFoundError:
        return
    if _cfg_mtime is None:
        _cfg_mtime = m; return
    if m != _cfg_mtime:
        _cfg_mtime = m
        try:
            new_cfg = load_config(path)
            apply_runtime_safe_updates(cfg_ref, new_cfg, logger_ref)
        except Exception as e:
            logger_ref.exception(f"config.yml 리로드 실패: {e}")

def notify(mode: str, text: str, doc: dict, *, title: Optional[str] = None):
    """
    알림 통합 엔트리:
        1) Discord로 모드별 채널 전송 시도
        2) 실패 시 기존 카카오로 백업
    """
    ncfg = (doc.get("notify") or {})
    pref = str(ncfg.get("use", "")).lower()

    discord_enabled = bool(((ncfg.get("discord") or {}).get("enabled", False)))
    kakao_enabled   = bool(((ncfg.get("kakao")   or {}).get("enabled", False)))

    def try_discord() -> bool:
        if not discord_enabled:
            return False
        return discord_notify.send_discord(mode=mode, text=text, doc=doc, title=title)
    
    def try_kakao() -> bool:
        if not kakao_enabled:
            return False
        try:
            send_kakao_text(text)
            return True
        except Exception:
            return False
        
    if pref == "discord":
        if try_discord(): return
        try_kakao(); return
    
    if pref == "kakao":
        if try_kakao(): return
        try_discord(); return
    
    if pref == "both":
        ok = try_discord()
        ok2 = try_kakao()
        return
    
    if discord_enabled:
        if try_discord(): return
    if kakao_enabled:
        try_kakao()    

# ======================
# 런처 (다중 모드 실행)
# ======================
def run_all_modes(logs_root: Optional[str]):
    modes = ["basic", "volatility", "volume", "rsi", "rsi_trend"]
    procs = []
    script = os.path.abspath(__file__)
    for m in modes:
        cmd = [sys.executable, script, "--mode", m]
        if logs_root:
            cmd += ["--logs-root", logs_root]
        proc = subprocess.Popen(cmd)
        procs.append((m, proc))
        print(f"[LAUNCH] mode={m} pid={proc.pid}")

    try:
        while True:
            alive = [p for _, p in procs if p.poll() is None]
            if not alive:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("[ALL] KeyboardInterrupt received, terminating child processes...")
        for m, p in procs:
            try:
                p.terminate()
                print(f"[ALL] terminate sent to {m} pid={p.pid}")
            except Exception:
                pass
        time.sleep(2)
        for m, p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                    print(f"[ALL] kill sent to {m} pid={p.pid}")
                except Exception:
                    pass
    finally:
        for _, p in procs:
            try:
                p.wait(timeout=1)
            except Exception:
                pass

# ======================
# 메인
# ======================
def main(args):
    mode = args.mode
    if mode == "all":
        run_all_modes(args.logs_root)
        return
    global LOG_DIR, STATE_FILE, PAPER_STATE, state, sim_balance, logger, action_logger, log, REST_FAIL_UNTIL, STRAT_TAG

    # 1) 설정 로드/배너
    CFG = load_config()
    BANNER = load_banner()

    # 2) 모드별 로그 디렉토리 확정
    logs_root = args.logs_root or os.getenv("COINTRADE_LOGS_ROOT") or BASE_DIR
    mode_log_dir = pick_log_dir_by_mode(mode)
    effective_log_dir = _abspath_under_base(logs_root, mode_log_dir)
    LOG_DIR = effective_log_dir
    STATE_FILE  = os.path.join(LOG_DIR, "state.json")
    PAPER_STATE = os.path.join(LOG_DIR, "paper_state.json")
    CFG.log_dir = LOG_DIR
    os.makedirs(CFG.log_dir, exist_ok=True)
    STRAT_TAG = f"[{mode.upper()}]"

    # 3) 로거 초기화(모드별 경로)
    # upbit-bot
    logger = logging.getLogger("upbit-bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    console_handler = logging.StreamHandler()
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(CFG.log_dir, "trade.log"),
        when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d"
    fmt = "%(asctime)s | %(levelname)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    console_handler.setFormatter(logging.Formatter(fmt, datefmt))
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    # trade-action
    action_logger = logging.getLogger("trade-action")
    action_logger.setLevel(logging.INFO)
    action_logger.propagate = False
    action_logger.handlers.clear()
    action_file_handler = logging.FileHandler(
        filename=os.path.join(CFG.log_dir, "trade_action.log"),
        encoding="utf-8", mode="a"
    )
    action_fmt = "%(asctime)s | %(message)s"
    action_file_handler.setFormatter(logging.Formatter(action_fmt, datefmt))
    action_logger.addHandler(action_file_handler)

    log = logger

    # 4) 인증/REST 쿨다운 초기화
    load_dotenv(ENV_FILE)
    ACCESS = os.getenv("UPBIT_ACCESS")
    SECRET = os.getenv("UPBIT_SECRET")
    upbit = None
    if not CFG.paper_trade:
        if not ACCESS or not SECRET:
            raise SystemExit("실거래 모드인데 /config/.env에 UPBIT_ACCESS/UPBIT_SECRET가 없습니다.")
        upbit = pyupbit.Upbit(ACCESS, SECRET)
    REST_FAIL_UNTIL = 0.0

    # 5) 상태/모의잔고 파일 (모드별 폴더로 분리 저장)
    state = read_json(STATE_FILE, {
        "op_code": "",
        "in_position": False,
        "entry_avg": 0.0,
        "last_action": "",
        "last_action_ts": 0.0,
        "ladder_idx": 0,
        "last_daily_date": ""
    })
    if CFG.paper_trade:
        paper_loaded = read_json(PAPER_STATE, None)
        sim_balance = paper_loaded if paper_loaded else {"KRW": CFG.seed_krw, "coin": 0.0, "avg": 0.0}
        if not paper_loaded:
            write_json(PAPER_STATE, sim_balance)
    else:
        sim_balance = {"KRW": 0.0, "coin": 0.0, "avg": 0.0}

    # 6) 배너
    if BANNER.strip():
        log.info("\n" + BANNER)

    # 7) WebSocket
    ps = PriceStream([CFG.ticker])
    ps.start()

    # 8) 시작 시 상태 동기화
    krw0, coin0, avg0 = get_krw_and_coin_balance(CFG, upbit)
    with state_lock:
        actually_in_pos = (coin0 > 0.0)
        if actually_in_pos and not state["in_position"]:
            state["in_position"] = True
            state["op_code"] = state["op_code"] or new_op_code(CFG)
            state["entry_avg"] = avg0 or state["entry_avg"]
            write_json(STATE_FILE, state)
        if (not actually_in_pos) and state["in_position"]:
            state["in_position"] = False
            state["op_code"] = ""
            write_json(STATE_FILE, state)

    # 9) 설정 요약
    unit_krw_preview = calc_unit_krw_by_initial_balance(CFG, upbit)
    rows = [
        ("전략 모드",          mode),
        ("거래 코인",          CFG.ticker),
        ("시드머니",           f"{CFG.seed_krw:,.0f} KRW"),
        ("분할 횟수",          f"{CFG.parts} (1회분량 ≈ {unit_krw_preview:,.0f} KRW)"),
        ("추가매수 트리거",     f"-{CFG.drop_pct*100:.2f}%"),
        ("전량매도 트리거",     f"+{CFG.rise_pct*100:.2f}%"),
        ("최소 주문액",         f"{CFG.min_order_krw:,.0f} KRW"),
        ("쿨다운 시간",         f"{CFG.cooldown_sec} 초"),
        ("시간당 매수 제한",     f"{CFG.max_buys_per_hour} 회"),
        ("로그 주기",           f"{CFG.log_every_sec} 초"),
        ("WS Fallback",        f"{CFG.ws_stale_fallback_sec} 초"),
        ("모드",               ("모의거래" if CFG.paper_trade else "실거래")),
        ("로그 폴더",          CFG.log_dir),
    ]
    label_width = max(wcswidth(k) for k, _ in rows)
    log.info("===== Bot Configuration (from config/config.yml) =====")
    for k, v in rows:
        log.info(f"{pad_label(k, label_width)} : {v}")
    log.info("=====================================================")

    suppress_first_buy = False

    # 실제 모드
    if not CFG.paper_trade:
        if coin0 > 0.0:
            suppress_first_buy = True
    
    # 모의 모드
    else:
        if sim_balance.get("coin", 0.0) > 0.0:
            suppress_first_buy = True
        else:
            ACCESS = os.getenv("UPBIT_ACCESS")
            SECRET = os.getenv("UPBIT_SECRET")
            if ACCESS and SECRET:
                try:
                    _tmp = pyupbit.Upbit(ACCESS, SECRET)
                    _krwX, _coinX, _avgX = get_krw_and_coin_balance(CFG, _tmp)
                    if _coinX > 0.0:
                        suppress_first_buy = True
                        log.info("[INFO] 실보유 코인 감지(실거래 계정) -> 초매수 X")
                except Exception:
                    pass
    
    if suppress_first_buy:
        log.info("[INFO] 시작 시 보유 코인 감지 -> 첫 매수 비활성화")

    last_action_ts = 0.0
    next_log_ts = 0.0
    next_cfg_check_ts = 0.0

    # 10) F&G Index
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        doc = {}
        log.warning(f"config 파일을 찾을 수 없습니다: {CONFIG_FILE}")
    except Exception as e:
        doc = {}
        log.exception(f"config.yml 파싱 실패: {e}")

    fgi = (doc.get("fear_greed") or {})
    run_in_modes = set(fgi.get("run_in_modes") or ["basic"])
    FG_ENABLED = bool(fgi.get("enabled", False)) and (mode in run_in_modes)
    FG_MODE = str(fgi.get("mode", "daily")).lower()
    FG_DAILY_HOUR = int(fgi.get("daily_kst_hour", 9))
    FG_INTERVAL = int(fgi.get("interval_sec", 3600))
    FG_NOTIFY = bool(fgi.get("notify_kakao", False))
    FG_NOTIFY_START = bool(fgi.get("notify_on_start", True))
    FG_LOGFILE = fgi.get("log_file", "fg_index.log")

    fg_worker = None
    if FG_ENABLED:
        fg_worker = FearGreedWorker(
            logs_dir=CFG.log_dir,
            log_file=FG_LOGFILE,
            interval_sec=FG_INTERVAL,
            notify_kakao=FG_NOTIFY,
            logger=log,
            mode=FG_MODE,
            daily_kst_hour=FG_DAILY_HOUR,
            notify_on_start=FG_NOTIFY_START
        )
        fg_worker.start()

    # 11) 모드별 준비물
    rsi_buf = RSIBuffer(period=14) if mode == "rsi" else None
    if mode == "volatility":
        vol_cfg = (doc.get("volatility") or {})
        VOL_WINDOW     = int(vol_cfg.get("window_sec", 300))
        VOL_REF_PCT    = float(vol_cfg.get("ref_vol_pct", 0.5))
        VOL_MIN_SCALE  = float(vol_cfg.get("min_scale", 0.6))
        VOL_MAX_SCALE  = float(vol_cfg.get("max_scale", 2.0))
        vol_guard = VolatilityGuard(window_sec=VOL_WINDOW)
    else:
        vol_guard = None
        VOL_REF_PCT = VOL_MIN_SCALE = VOL_MAX_SCALE = None

    if mode == "rsi_trend":
        trend_cfg = (doc.get("rsi_trend") or {})
        TR_RSI_PERIOD   = int(trend_cfg.get("rsi_period", 14))
        TR_RSI_BUY      = float(trend_cfg.get("rsi_buy", 32.0))
        TR_RSI_SELL     = float(trend_cfg.get("rsi_sell", 65.0))
        TR_EMA_SHORT    = int(trend_cfg.get("ema_short", 5))
        TR_EMA_LONG     = int(trend_cfg.get("ema_long", 20))
        TR_LOOKBACK     = int(trend_cfg.get("rebound_lookback", 5))
        TR_SEED_INIT    = bool(trend_cfg.get("use_init_seed_ohlcv", True))
        TR_PARTS        = int(trend_cfg.get("parts", CFG.parts))
        TR_DROP_PCT     = float(trend_cfg.get("drop_pct", CFG.drop_pct))
        TR_RISE_PCT     = float(trend_cfg.get("rise_pct", CFG.rise_pct))
        TR_MIN_HOLD   = int(trend_cfg.get("min_hold_sec", 180))
        TR_HARD_STOP  = float(trend_cfg.get("hard_stop_pct", 0.010))
        TR_TRAIL_STOP = float(trend_cfg.get("trail_stop_pct", 0.008))
        TR_TREND_SUST = int(trend_cfg.get("trend_down_sustain_sec", 25))

        # 루프 밖에서 추세 지속 체크용 타임스탬프
        trend_down_since = 0.0

        # RSI period 반영
        rsi_buf = RSIBuffer(period=TR_RSI_PERIOD)

        ema_s = EMACalc(TR_EMA_SHORT)
        ema_l = EMACalc(TR_EMA_LONG)
        rebound = ReboundDetector(lookback_ticks=TR_LOOKBACK)

        # --- Weighted ladder config (선택) ---
        TR_WEIGHTS = trend_cfg.get("unit_weights", None)
        if TR_WEIGHTS and isinstance(TR_WEIGHTS, list) and len(TR_WEIGHTS) > 0:
            UNIT_WEIGHTS = [max(0.0, float(w)) for w in TR_WEIGHTS]
            WEIGHT_SUM = sum(UNIT_WEIGHTS) if sum(UNIT_WEIGHTS) > 0 else None
        else:
            UNIT_WEIGHTS = None
            WEIGHT_SUM = None

        TR_DROP_LADDER = trend_cfg.get("drop_ladder_pct", None)
        if TR_DROP_LADDER and isinstance(TR_DROP_LADDER, list) and len(TR_DROP_LADDER) > 0:
            DROP_LADDER = [max(0.0, float(p)) for p in TR_DROP_LADDER]
        else:
            DROP_LADDER = None

        # 초기 seed (1분봉 종가 기반)
        if TR_SEED_INIT:
            try:
                _df = pyupbit.get_ohlcv(CFG.ticker, interval="minute1", count=max(TR_EMA_LONG*3, 60))
                if _df is not None and len(_df) > 0:
                    closes = [float(c) for c in _df["close"].tolist() if c and c > 0]
                    if closes:
                        # EMA 초기화는 SMA로 seed 후 최근부터 숫자 적용
                        seed_len = min(len(closes), TR_EMA_LONG)
                        ema_s.seed(closes[:seed_len])
                        ema_l.seed(closes[:seed_len])
                        for px in closes[seed_len:]:
                            ema_s.update(px)
                            ema_l.update(px)
                        # rsi도 시도
                        for px in closes[-(TR_RSI_PERIOD+2):]:
                            rsi_buf.add(px)
            except Exception as e:
                log.warning(f"[rsi_trend] 초기 seed 실패: {e}")

    # 12) 매일 09:00 알림
    next_daily_ts = next_kst_0900().timestamp()

    # 13) 루프
    try:
        while True:
            now = time.time()

            # 설정 리로드 (5초)
            if now >= next_cfg_check_ts:
                maybe_reload_config(CFG, log, CONFIG_FILE)
                next_cfg_check_ts = now + 5.0

            # 가격 (WS 우선)
            ps_price = PriceStream.get_last_static(CFG.ticker) if hasattr(PriceStream, "get_last_static") else None
            # 일부 PriceStream 구현엔 static 헬퍼가 없을 수 있어 기존 인스턴스 접근
            if ps_price is None:
                # 인스턴스 접근: stale 체크 위해 재사용
                # get_last는 아래에서 ps 참조로 다시 가져온다
                pass

            # 웹소켓/REST 혼합
            price_ws = None
            try:
                price_ws = ps.get_last(CFG.ticker)
            except Exception:
                price_ws = None
            if price_ws is None or ps.last_recv_age() > CFG.ws_stale_fallback_sec:
                price = get_rest_price(CFG.ticker)
            else:
                price = price_ws

            if vol_guard:
                vol_guard.add(now, price if price else 0.0)

            if not is_valid_price(price):
                log.warning("[시세] 유효한 금액이 아님")
                time.sleep(CFG.poll_sec)
                continue

            # 잔고
            krw, coin_bal, avg = get_krw_and_coin_balance(CFG, upbit)

            # 매일 09:00 요약
            if now >= next_daily_ts:
                today_kst = now_kst().date().isoformat()
                with state_lock:
                    last_sent = state.get("last_daily_date", "")
                    op = state.get("op_code") or "-"
                if last_sent != today_kst:
                    msg = (
                        f"[{STRAT_TAG} 일일 요약] {now_kst().strftime('%Y-%m-%d (%a) %H:%M KST')}\n"
                        f"티커: {CFG.ticker}\n"
                        f"현재가: {price:,.0f}\n"
                        f"보유수량: {coin_bal:.8f}\n"
                        f"평단: {avg:,.0f}\n"
                        f"등락률(P/L): {fmt_pct(price, avg)}\n"
                        f"현금잔고(KRW): {krw:,.0f}\n"
                        f"포지션 OP: {op}\n"
                        f"(기준: 한국시간 오전 9시)"
                    )
                    log.info("[일일 요약 발송] 한국 시간 09:00")
                    try:
                        notify(mode, msg, doc, title="일일 요약")
                    except Exception as e:
                        log.exception(f"알림 전송 실패: {e}")
                    with state_lock:
                        state["last_daily_date"] = today_kst
                        write_json(STATE_FILE, state)
                next_daily_ts = next_kst_0900().timestamp()

            # 상태 로그 (cron_log or N초 단위)
            now_dt = datetime.fromtimestamp(now, tz=KST)

            should_log = False

            if CFG.cron_log:
                if is_cron_match(CFG.cron_log, now_dt) and now >= next_log_ts:
                    should_log = True

            else:
                if now >= next_log_ts:
                    should_log = True
            
            if should_log:
                with state_lock:
                    op = state["op_code"] or "-"
                    krw_str = fmt_krw(krw)
                if avg > 0:
                    pl_str = fmt_pct(price, avg)
                    log.info(f"[{op}] 가격={price:,.0f} | 평단={avg:,.0f} | 등락률={pl_str} | 잔고={krw_str}")
                else:
                    log.info(f"[{op}] 가격={price:,.0f} | 평단=- | 등락률=- | 잔고={krw_str}")
                    
                # rsi_trend 보조지표 출력
                if mode == "rsi_trend":
                    try:
                        log.info(f"[RSI_TREND] RSI={rsi_buf.rsi():.2f}  EMA5≈{ema_s_val:.1f}  EMA20≈{ema_l_val:.1f}")
                    except Exception:
                        pass
                    
                next_log_ts = now + 1.0 if CFG.cron_log else now + CFG.log_every_sec

            # 쿨다운
            if now - last_action_ts < CFG.cooldown_sec:
                time.sleep(CFG.poll_sec); continue

            # 전략 공통 준비
            base_for_unit = CFG.seed_krw if CFG.paper_trade else max(krw, CFG.min_order_krw)

            if mode == "rsi_trend":
                local_parts = TR_PARTS
                local_drop = TR_DROP_PCT
                local_rise = TR_RISE_PCT
            else:
                local_parts = CFG.parts
                local_drop = CFG.drop_pct
                local_rise = CFG.rise_pct

            unit_krw = max(CFG.min_order_krw, math.floor(base_for_unit / local_parts))
            with state_lock:
                in_pos = state["in_position"]

            drop_trig = local_drop
            rise_trig = local_rise

            # volatility 모드: 변동성 스케일링
            if mode == "volatility" and vol_guard:
                vol_pct = vol_guard.realized_vol_pct()
                scale = (vol_pct / VOL_REF_PCT) if (VOL_REF_PCT and VOL_REF_PCT > 0) else 1.0
                scale = max(VOL_MIN_SCALE, min(VOL_MAX_SCALE, scale))
                drop_trig = CFG.drop_pct * scale
                rise_trig = CFG.rise_pct * scale

            # 보조 유틸
            def try_first_buy():
                if not can_buy_now(CFG): return False
                if buy_unit_krw_with_available(CFG, upbit, unit_krw, krw, price, mode, doc):
                    record_buy_time()
                    return True
                return False
            def try_add_buy():
                if not can_buy_now(CFG): return False
                if buy_unit_krw_with_available(CFG, upbit, unit_krw, krw, price, mode, doc):
                    record_buy_time()
                    return True
                return False
            def try_sell_all():
                return sell_all_market(CFG, upbit, coin_bal, price, mode, doc)
            def try_first_buy_amount(amount: float) -> bool:
                if not can_buy_now(CFG): return False
                if buy_unit_krw_with_available(CFG, upbit, amount, krw, price, mode, doc):
                    record_buy_time(); return True
                return False
            def try_add_buy_amount(amount: float) -> bool:
                if not can_buy_now(CFG): return False
                if buy_unit_krw_with_available(CFG, upbit, amount, krw, price, mode, doc):
                    record_buy_time(); return True
                return False

            acted = False

            if mode in ("basic", "volatility"):
                if not in_pos and coin_bal == 0 and not suppress_first_buy:
                    if try_first_buy():
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price <= avg * (1 - drop_trig):
                    if try_add_buy():
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price >= avg * (1 + rise_trig):
                    if try_sell_all():
                        suppress_first_buy = False
                        last_action_ts = time.time(); acted = True

            elif mode == "rsi":
                if rsi_buf: rsi_buf.add(price)
                rsi_val = rsi_buf.rsi() if rsi_buf else 50.0
                if not in_pos and coin_bal == 0 and not suppress_first_buy:
                    if rsi_val <= 30.0 and try_first_buy():
                        log.info(f"[RSI] RSI={rsi_val:.2f} → 초매수")
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price <= avg * (1 - drop_trig):
                    if rsi_val <= 35.0 and try_add_buy():
                        log.info(f"[RSI] RSI={rsi_val:.2f} & 하락 트리거 → 추가매수")
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price >= avg * (1 + rise_trig):
                    if rsi_val >= 65.0 and try_sell_all():
                        log.info(f"[RSI] RSI={rsi_val:.2f} & 상승 트리거 → 전량매도")
                        suppress_first_buy = False
                        last_action_ts = time.time(); acted = True

            elif mode == "volume":
                vr = get_volume_ratio(CFG.ticker, count=30)
                VR_TH = 1.5
                if not in_pos and coin_bal == 0 and not suppress_first_buy:
                    if vr >= VR_TH and try_first_buy():
                        log.info(f"[VOL] volume_ratio={vr:.2f} → 초매수")
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price <= avg * (1 - drop_trig):
                    if vr >= VR_TH and try_add_buy():
                        log.info(f"[VOL] volume_ratio={vr:.2f} & 하락 트리거 → 추가매수")
                        last_action_ts = time.time(); acted = True
                elif avg > 0 and price >= avg * (1 + rise_trig):
                    if vr >= VR_TH and try_sell_all():
                        log.info(f"[VOL] volume_ratio={vr:.2f} & 상승 트리거 → 전량매도")
                        suppress_first_buy = False
                        last_action_ts = time.time(); acted = True
            
            elif mode == "rsi_trend":
                # 보조지표 업데이트
                if rsi_buf: rsi_buf.add(price)

                # EMA 갱신
                try:
                    ema_s_val = ema_s.update(price)
                    ema_l_val = ema_l.update(price)
                except NameError:
                    ema_s_val = price
                    ema_l_val = price

                rsi_val = rsi_buf.rsi() if rsi_buf else 50.0
                trend_up = (ema_s_val > ema_l_val)

                # 반등 시그널: 최근 short 아래였고, 지금 price>=short, 그리고 short>long
                try:
                    is_rebound = rebound.update_and_check(price, ema_s_val, ema_l_val)  # type: ignore[name-defined]
                except NameError:
                    is_rebound = False

                # === 사다리 현재 단계 조회 ===
                with state_lock:
                    cur_idx = int(state.get("ladder_idx", 0))
                    cur_in_pos = bool(state.get("in_position", False))

                # === 단계별 단위금액 계산 ===
                if UNIT_WEIGHTS and WEIGHT_SUM and WEIGHT_SUM > 0:
                    idx_for_unit = 0 if (not cur_in_pos) else min(cur_idx, len(UNIT_WEIGHTS)-1)
                    step_weight = UNIT_WEIGHTS[idx_for_unit]
                    step_unit_krw = max(CFG.min_order_krw, math.floor((base_for_unit * step_weight) / WEIGHT_SUM))
                else:
                    # 가중치 미설정 → 기존 균등 분할
                    step_unit_krw = max(CFG.min_order_krw, math.floor(base_for_unit / local_parts))

                # === 단계별 드랍 트리거 ===
                if DROP_LADDER:
                    idx_for_drop = min(cur_idx, len(DROP_LADDER)-1)
                    drop_trig_local = DROP_LADDER[idx_for_drop]
                else:
                    drop_trig_local = drop_trig  # 기존 값 사용

                acted_local = False

                with state_lock:
                        if state.get("in_position", False):
                            cur_peak = float(state.get("peak_price", 0.0) or 0.0)
                            if price > cur_peak:
                                state["peak_price"] = float(price)

                # === 최초 진입 ===
                if not in_pos and coin_bal == 0 and not suppress_first_buy:
                    if (rsi_val <= TR_RSI_BUY) and trend_up and is_rebound:
                        if try_first_buy_amount(step_unit_krw):
                            with state_lock:
                                state["ladder_idx"] = 0   # 초매수 이후 다음 추가는 0단계 기준부터 시작
                                state["entry_ts"] = time.time()
                                state["peak_price"] = float(price)
                                write_json(STATE_FILE, state)
                            log.info(f"[RSI_TREND] 초매수: RSI={rsi_val:.2f}, unit={step_unit_krw:,.0f} KRW, idx=0")
                            last_action_ts = time.time(); acted_local = True

                # === 추가매수 ===
                elif avg > 0 and price <= avg * (1 - drop_trig_local):
                    if (rsi_val <= min(40.0, TR_RSI_BUY + 5.0)) and trend_up and is_rebound:
                        # 다음 체결 금액(현 단계 기준)
                        if UNIT_WEIGHTS and WEIGHT_SUM:
                            idx_for_unit = min(cur_idx, len(UNIT_WEIGHTS)-1)
                            step_weight = UNIT_WEIGHTS[idx_for_unit]
                            step_unit_krw = max(CFG.min_order_krw, math.floor((base_for_unit * step_weight) / WEIGHT_SUM))
                        else:
                            step_unit_krw = max(CFG.min_order_krw, math.floor(base_for_unit / local_parts))

                        if try_add_buy_amount(step_unit_krw):
                            with state_lock:
                                # 성공 시 단계 +1 (최대 길이-1까지만)
                                max_len = max(
                                    len(UNIT_WEIGHTS) if UNIT_WEIGHTS else 0,
                                    len(DROP_LADDER)  if DROP_LADDER  else 0
                                )
                                if max_len <= 0:
                                    state["ladder_idx"] = cur_idx + 1
                                else:
                                    state["ladder_idx"] = min(cur_idx + 1, max_len - 1)
                                new_idx = state["ladder_idx"]
                                prev_peak = float(state.get("peak_price", 0.0) or 0.0)
                                state["peak_price"] = max(prev_peak, float(price))
                                write_json(STATE_FILE, state)
                            log.info(f"[RSI_TREND] 추가매수: RSI={rsi_val:.2f}, unit={step_unit_krw:,.0f} KRW, idx->{new_idx}")
                            last_action_ts = time.time(); acted_local = True

                # === 매도(익절/방어) ===
                elif avg > 0:
                    # === 지표/상태 ===
                    with state_lock:
                        entry_ts  = float(state.get("entry_ts", 0.0) or 0.0)
                        peak_px   = float(state.get("peak_price", 0.0) or 0.0)
                    held_sec = (time.time() - entry_ts) if entry_ts > 0 else 1e9
                    profit   = (price / avg - 1.0)
                    drawdown = ((peak_px - price) / peak_px) if (peak_px and peak_px > 0) else 0.0

                    # === 하락 추세 '지속' 여부 ===
                    if ema_s_val < ema_l_val:
                        if trend_down_since == 0.0:
                            trend_down_since = time.time()
                    else:
                        trend_down_since = 0.0
                    trend_down_sustained = (trend_down_since > 0.0) and ((time.time() - trend_down_since) >= TR_TREND_SUST)

                    # === 익절: 상승 모멘텀 유지 중에만 ===
                    take_profit = (
                        (price >= avg * (1 + rise_trig)) and
                        (rsi_val >= TR_RSI_SELL) and
                        (ema_s_val >= ema_l_val)
                    )

                    # === 방어: 최소 보유시간 이후에만 작동 ===
                    hard_stop = (held_sec >= TR_MIN_HOLD) and (profit <= -TR_HARD_STOP)

                    trailing_stop = (
                        (held_sec >= TR_MIN_HOLD) and
                        (profit > 0.0) and
                        (drawdown >= TR_TRAIL_STOP) and
                        trend_down_sustained
                    )

                    should_exit = take_profit or hard_stop or trailing_stop

                    if should_exit:
                        if try_sell_all():
                            # 재진입 허용
                            suppress_first_buy = False
                            # 상태 정리
                            with state_lock:
                                state["entry_ts"] = 0.0
                                state["peak_price"] = 0.0
                                write_json(STATE_FILE, state)
                            log.info(
                                f"[RSI_TREND] EXIT: "
                                f"{'TP' if take_profit else ('HARD_STOP' if hard_stop else 'TRAIL_STOP')} | "
                                f"held={held_sec:.0f}s profit={profit*100:.2f}% dd={drawdown*100:.2f}%"
                            )
                            last_action_ts = time.time(); acted_local = True


                if acted_local:
                    time.sleep(CFG.poll_sec)
                    continue

            if acted:
                time.sleep(CFG.poll_sec)
                continue

            time.sleep(CFG.poll_sec)

    except KeyboardInterrupt:
        log.info("종료 요청(KeyboardInterrupt)")
    except Exception as e:
        log.exception(f"오류: {e}")
    finally:
        try:
            if 'fg_worker' in locals() and fg_worker:
                fg_worker.stop()
        except Exception:
            pass
        try:
            ps.stop()
        except Exception:
            pass
        log.info("정상 종료")

if __name__ == "__main__":
    args = parse_args()
    # If run without arguments, default to launching all modes
    if len(sys.argv) == 1:
        args.mode = "all"
    main(args)
