# coinTrade2.py
from dataclasses import dataclass
from typing import Tuple
import os, sys, math, time, json, uuid, logging, threading
from logging.handlers import TimedRotatingFileHandler
from kakao_notify import send_kakao_text
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from fg_index import FearGreedWorker

from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
import pyupbit

from price_source import PriceStream

# ======================
# 경로 상수
# ======================
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR   = os.path.join(BASE_DIR, "config")
CONFIG_FILE  = os.path.join(CONFIG_DIR, "config.yml")
BANNER_FILE  = os.path.join(CONFIG_DIR, "banner.txt")
ENV_FILE     = os.path.join(CONFIG_DIR, ".env")
LOG_DIR      = os.path.join(BASE_DIR, "logs")
STATE_FILE   = os.path.join(LOG_DIR, "state.json")         # 포지션 상태
PAPER_STATE  = os.path.join(LOG_DIR, "paper_state.json")   # 모의 잔고 상태

# ======================
# 전역 상수
# ======================
REST_COOLDOWN_SEC = 600.0   # 기본 10분 (config에서 덮어씀)
REST_FAIL_UNTIL = 0.0       # 실패 후 다시 시도 가능한 시각

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
    poll_sec: float
    log_every_sec: float
    paper_trade: bool
    log_dir: str
    ws_stale_fallback_sec: float

def load_config(path: str = CONFIG_FILE) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    t = doc.get("trade", {}) or {}

    # upbit config 로드
    upbit_cfg = doc.get("upbit") or {}
    global REST_COOLDOWN_SEC
    REST_COOLDOWN_SEC = float(upbit_cfg.get("rest_cooldown_sec", REST_COOLDOWN_SEC))

    return Config(
        ticker=t.get("ticker", "KRW-BTC"),
        seed_krw=float(t.get("seed_krw", 1_000_000)),
        parts=int(t.get("parts", 40)),
        drop_pct=float(t.get("drop_pct", 0.01)),
        rise_pct=float(t.get("rise_pct", 0.03)),
        min_order_krw=float(t.get("min_order_krw", 5000)),
        cooldown_sec=float(t.get("cooldown_sec", 20.0)),
        poll_sec=float(t.get("poll_sec", 1.0)),
        log_every_sec=float(t.get("log_every_sec", 5.0)),
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

CFG = load_config()
BANNER = load_banner()

# ======================
# 로깅
# ======================
os.makedirs(CFG.log_dir, exist_ok=True)

logger = logging.getLogger("upbit-bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(CFG.log_dir, "trade.log"),
    when="midnight", interval=1, backupCount=7, encoding="utf-8"
)
file_handler.suffix = "%Y-%m-%d"

# 포매터 (datefmt로 시간 형식 지정)
fmt = "%(asctime)s | %(levelname)s | %(message)s"
datefmt = "%Y-%m-%d %H:%M:%S"
console_handler.setFormatter(logging.Formatter(fmt, datefmt))
file_handler.setFormatter(logging.Formatter(fmt, datefmt))

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# 매수/매도 전용 로그
action_logger = logging.getLogger("trade-action")
action_logger.setLevel(logging.INFO)

# 중복 핸들러 방지(재시작/리로드 대비)
action_logger.propagate = False
action_logger.handlers.clear()

action_file_handler = logging.FileHandler(
    filename=os.path.join(CFG.log_dir, "trade_action.log"),
    encoding="utf-8",
    mode="a",  # 계속 이어쓰기
)
action_fmt = "%(asctime)s | %(message)s"
datefmt = "%Y-%m-%d %H:%M:%S"
action_file_handler.setFormatter(logging.Formatter(action_fmt, datefmt))
action_logger.addHandler(action_file_handler)

log = logger

# ======================
# 인증(실거래만)
# ======================
load_dotenv(ENV_FILE)
ACCESS = os.getenv("UPBIT_ACCESS")
SECRET = os.getenv("UPBIT_SECRET")
upbit = None
if not CFG.paper_trade:
    if not ACCESS or not SECRET:
        raise SystemExit("실거래 모드인데 /config/.env에 UPBIT_ACCESS/UPBIT_SECRET가 없습니다.")
    upbit = pyupbit.Upbit(ACCESS, SECRET)

# ======================
# 공통 유틸
# ======================
def fmt_pct(price: float, avg: float) -> str:
    if avg and avg > 0:
        return f"{(price/avg - 1)*100:+.2f}%"
    return "-"

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
        # 10분 동안 REST 호출 중단
        REST_FAIL_UNTIL = now + REST_COOLDOWN_SEC
        return 0.0

def new_op_code() -> str:
    base = CFG.ticker.replace("-", "")
    return f"{base}-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

KST = ZoneInfo("Asia/Seoul")
def now_kst() -> datetime:
    return datetime.now(tz=KST)

def next_kst_0900(after: Optional[datetime] = None) -> datetime:
    """기준 시각(after) 이후 '다음' 한국시간 오전 9시 반환."""
    base = after.astimezone(KST) if after else now_kst()
    target_today = base.replace(hour=9, minute=0, second=0, microsecond=0)
    if base < target_today:
        return target_today
    return (target_today + timedelta(days=1))

def format_daily_summary(op_code: str, price: float, krw: float, coin: float, avg: float) -> str:
    pl = "-"
    if avg and avg > 0:
        pl = f"{(price/avg - 1)*100:+.2f}%"
    return (
        f"[일일 요약] {now_kst().strftime('%Y-%m-%d (%a) %H:%M KST')}\n"
        f"티커: {CFG.ticker}\n"
        f"현재가: {price:,.0f}\n"
        f"보유수량: {coin:.8f}\n"
        f"평단: {avg:,.0f}\n"
        f"등락률(P/L): {pl}\n"
        f"현금잔고(KRW): {krw:,.0f}\n"
        f"포지션 OP: {op_code or '-'}\n"
        f"(기준: 한국시간 오전 9시)"
    )

# ======================
# 실행 상태 (영속)
# ======================
state_lock = threading.Lock()
state = read_json(STATE_FILE, {
    "op_code": "",
    "in_position": False,
    "entry_avg": 0.0,
    "last_action": "",
    "last_action_ts": 0.0
})

# 모의 잔고
if CFG.paper_trade:
    paper_loaded = read_json(PAPER_STATE, None)
    if paper_loaded:
        sim_balance = paper_loaded
    else:
        sim_balance = {"KRW": CFG.seed_krw, "coin": 0.0, "avg": 0.0}
else:
    sim_balance = {"KRW": 0.0, "coin": 0.0, "avg": 0.0}

# ======================
# 잔고 조회
# ======================
def get_krw_and_coin_balance(ticker: str) -> Tuple[float, float, float]:
    global REST_FAIL_UNTIL
    if CFG.paper_trade:
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
        base = ticker.split("-")[1]
        for b in balances:
            if b.get("currency") == "KRW":
                krw = float(b.get("balance", 0) or 0)
            if b.get("currency") == base:
                coin_bal = float(b.get("balance", 0) or 0)
                avg = float(b.get("avg_buy_price", 0) or 0)
        return krw, coin_bal, avg

# ======================
# 주문
# ======================
def buy_unit_krw_with_available(unit_krw: float, available_krw: float, price: float) -> bool:
    amt = math.floor(min(unit_krw, available_krw))
    if amt < CFG.min_order_krw:
        log.info(f"매수 스킵: 가용 KRW {available_krw:,.0f} < 최소주문액 {CFG.min_order_krw:,.0f}")
        return False

    if CFG.paper_trade:
        volume = amt / price
        prev_coin = sim_balance["coin"]
        prev_cost = sim_balance["avg"] * prev_coin
        new_coin = prev_coin + volume
        sim_balance["coin"] = new_coin
        sim_balance["KRW"] -= amt
        sim_balance["avg"] = (prev_cost + amt) / new_coin if new_coin > 0 else 0
        with state_lock:
            if not state["in_position"]:
                state["op_code"] = new_op_code()
                state["in_position"] = True
            state["entry_avg"] = sim_balance["avg"]
            state["last_action"] = "BUY"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        write_json(PAPER_STATE, sim_balance)
        msg = (f"[{state['op_code']}] [모의] 매수 {amt:,.0f} KRW "
               f"→ 평단={sim_balance['avg']:,.0f}, "
               f"등락률={fmt_pct(price, sim_balance['avg'])}")
        log.info(msg); action_logger.info(msg)
        send_kakao_text(msg)
        return True
    else:
        r = upbit.buy_market_order(CFG.ticker, amt)
        ok = bool(r and r.get("uuid"))
        # 주문 후 평균단가 재조회
        _, _, avg_after = get_krw_and_coin_balance(CFG.ticker)
        with state_lock:
            if ok and not state["in_position"]:
                state["op_code"] = new_op_code()
                state["in_position"] = True
            if ok:
                state["entry_avg"] = avg_after
            state["last_action"] = "BUY" if ok else "BUY_FAIL"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        msg = f"[{state['op_code'] or '-'}] 시장가 매수 {amt:,.0f} KRW → {'성공' if ok else '실패'} ({r})"
        log.info(msg); action_logger.info(msg)
        send_kakao_text(msg)
        return ok

def sell_all_market(volume: float, price: float) -> bool:
    if volume <= 0:
        log.info("매도 스킵: 보유수량 0")
        return False

    if CFG.paper_trade:
        krw_gain = volume * price
        prev_avg = sim_balance["avg"]
        with state_lock:
            op = state["op_code"] or new_op_code()
        sim_balance["KRW"] += krw_gain
        sim_balance["coin"] = 0.0
        sim_balance["avg"] = 0.0
        with state_lock:
            state["in_position"] = False
            state["last_action"] = "SELL"
            state["last_action_ts"] = time.time()
            closed_op = state["op_code"]
            state["op_code"] = ""
            write_json(STATE_FILE, state)
        write_json(PAPER_STATE, sim_balance)
        msg = (f"[{closed_op or op}] [모의] 전량매도 {volume:.8f} coin @ {price:,.0f} → "
               f"잔고={sim_balance['KRW']:,.0f} (직전 등락률 {fmt_pct(price, prev_avg)})")
        log.info(msg); action_logger.info(msg)
        send_kakao_text(msg)
        return True
    else:
        r = upbit.sell_market_order(CFG.ticker, volume)
        ok = bool(r and r.get("uuid"))
        with state_lock:
            op = state["op_code"] or new_op_code()
            if ok:
                state["in_position"] = False
                closed_op = state["op_code"]
                state["op_code"] = ""
            else:
                closed_op = state["op_code"]
            state["last_action"] = "SELL" if ok else "SELL_FAIL"
            state["last_action_ts"] = time.time()
            write_json(STATE_FILE, state)
        msg = f"[{closed_op or op}] 시장가 전량매도 {volume:.8f} → {'성공' if ok else '실패'} ({r})"
        log.info(msg); action_logger.info(msg)
        send_kakao_text(msg)
        return ok

def calc_unit_krw_by_initial_balance() -> float:
    krw, _, _ = get_krw_and_coin_balance(CFG.ticker)
    base = CFG.seed_krw if CFG.paper_trade else max(krw, CFG.min_order_krw)
    return max(CFG.min_order_krw, math.floor(base / CFG.parts))

# ======================
# 설정 자동 리로드 (안전 항목만)
# ======================
_cfg_lock = threading.Lock()
_cfg_mtime = None

def apply_runtime_safe_updates(old: Config, new: Config):
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
        if new.log_every_sec != old.log_every_sec:
            old.log_every_sec = new.log_every_sec; updated.append("log_every_sec")
        if new.ws_stale_fallback_sec != old.ws_stale_fallback_sec:
            old.ws_stale_fallback_sec = new.ws_stale_fallback_sec; updated.append("ws_stale_fallback_sec")

        # 중간 변경 비권장 항목 (경고만)
        for k in ["paper_trade","seed_krw","ticker","log_dir","poll_sec"]:
            if getattr(new, k) != getattr(old, k):
                log.warning(f"{k}는 실행 중 변경 비권장 → 무시")
    if updated:
        log.info(f"⚡ config.yml 변경 감지 → 적용됨: {', '.join(updated)}")

def maybe_reload_config(path: str = CONFIG_FILE):
    global _cfg_mtime
    try:
        m = os.path.getmtime(path)
    except FileNotFoundError:
        return
    if _cfg_mtime is None:
        _cfg_mtime = m
        return
    if m != _cfg_mtime:
        _cfg_mtime = m
        try:
            new_cfg = load_config(path)
            apply_runtime_safe_updates(CFG, new_cfg)
        except Exception as e:
            log.exception(f"config.yml 리로드 실패: {e}")

# ======================
# 메인
# ======================
def main():
    # 배너
    if BANNER.strip():
        log.info("\n" + BANNER)

    # WebSocket 시작
    ps = PriceStream([CFG.ticker])
    ps.start()

    # 시작 시 상태 동기화
    krw0, coin0, avg0 = get_krw_and_coin_balance(CFG.ticker)
    with state_lock:
        actually_in_pos = (coin0 > 0.0)
        if actually_in_pos and not state["in_position"]:
            state["in_position"] = True
            state["op_code"] = state["op_code"] or new_op_code()
            state["entry_avg"] = avg0 or state["entry_avg"]
            write_json(STATE_FILE, state)
        if (not actually_in_pos) and state["in_position"]:
            state["in_position"] = False
            state["op_code"] = ""
            write_json(STATE_FILE, state)

    # 설정 요약
    unit_krw = calc_unit_krw_by_initial_balance()
    log.info("===== Bot Configuration (from config/config.yml) =====")
    log.info(f"거래 코인     : {CFG.ticker}")
    log.info(f"시드머니      : {CFG.seed_krw:,.0f} KRW")
    log.info(f"분할 횟수     : {CFG.parts} (1회분량 ≈ {unit_krw:,.0f} KRW)")
    log.info(f"추가매수 트리거 : -{CFG.drop_pct*100:.2f}%")
    log.info(f"전량매도 트리거 : +{CFG.rise_pct*100:.2f}%")
    log.info(f"최소 주문액   : {CFG.min_order_krw:,.0f} KRW")
    log.info(f"쿨다운 시간   : {CFG.cooldown_sec} 초")
    log.info(f"로그 주기     : {CFG.log_every_sec} 초")
    log.info(f"WS Fallback   : {CFG.ws_stale_fallback_sec} 초")
    log.info(f"모드          : {'모의거래' if CFG.paper_trade else '실거래'}")
    log.info(f"로그 폴더     : {CFG.log_dir}")
    log.info("=====================================================")

    last_action_ts = 0.0
    next_log_ts = 0.0
    next_cfg_check_ts = 0.0

    # Fear & Greed Index
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
    FG_ENABLED = bool(fgi.get("enabled", False))
    FG_MODE = str(fgi.get("mode", "daily")).lower()
    FG_DAILY_HOUR = int(fgi.get("daily_kst_hour", 9))
    FG_INTERVAL = int(fgi.get("interval_sec", 3600))
    FG_NOTIFY = bool(fgi.get("notify_kakao", False))
    FG_NOTIFY_START = bool(fgi.get("notify_on_start", True))
    FG_LOGFILE = fgi.get("log_file", "fg_index.log")
    fg_worker = None

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

    # 다음 한국시간 09:00 계산
    next_daily_dt = next_kst_0900()
    next_daily_ts = next_daily_dt.timestamp()

    try:
        while True:
            now = time.time()

            # (1) 설정 리로드 (5초마다)
            if now >= next_cfg_check_ts:
                maybe_reload_config(CONFIG_FILE)
                next_cfg_check_ts = now + 5.0

            # (2) 가격: WS 우선, 멈추면 REST
            ps_price = ps.get_last(CFG.ticker)
            if ps_price is None or ps.last_recv_age() > CFG.ws_stale_fallback_sec:
                price = get_rest_price(CFG.ticker)
            else:
                price = ps_price

            # (3) 잔고
            krw, coin_bal, avg = get_krw_and_coin_balance(CFG.ticker)

            # (4) 매일 09:00 카카오 알림 전송
            # - 재시작 직후 중복 방지: state['last_daily_date']로 같은 날짜면 스킵
            if now >= next_daily_ts:
                today_kst = now_kst().date().isoformat()
                with state_lock:
                    last_sent = state.get("last_daily_date", "")
                    op = state.get("op_code") or "-"
                if last_sent != today_kst:
                    msg = format_daily_summary(op, price, krw, coin_bal, avg)
                    log.info("[일일 요약 발송] 한국 시간 09:00")
                    try:
                        from kakao_notify import send_kakao_text
                        send_kakao_text(msg)
                    except Exception as e:
                        log.exception(f"카카오 일일 요약 전송 실패: {e}")
                    # 발송 기록 저장
                    with state_lock:
                        state["last_daily_date"] = today_kst
                        write_json(STATE_FILE, state)
                # 다음 9시로 갱신
                nd = next_kst_0900()
                next_daily_ts = nd.timestamp()

            # (5) 상태 로그: 5초에 1번
            if now >= next_log_ts:
                with state_lock:
                    op = state["op_code"] or "-"
                if avg > 0:
                    log.info(f"[{op}] 가격={price:,.0f} | 평단={avg:,.0f} | 등락률={fmt_pct(price, avg)}")
                next_log_ts = now + CFG.log_every_sec

            # (6) 쿨다운
            if now - last_action_ts < CFG.cooldown_sec:
                time.sleep(CFG.poll_sec); continue

            # (7) 전략
            base_for_unit = CFG.seed_krw if CFG.paper_trade else max(krw, CFG.min_order_krw)
            unit_krw = max(CFG.min_order_krw, math.floor(base_for_unit / CFG.parts))

            with state_lock:
                in_pos = state["in_position"]

            # 1) 무포지션 → 초매수
            if not in_pos and coin_bal == 0:
                if buy_unit_krw_with_available(unit_krw, krw, price):
                    last_action_ts = time.time()
                time.sleep(CFG.poll_sec); continue

            # 2) 평단 대비 하락 → 추가매수
            if avg > 0 and price <= avg * (1 - CFG.drop_pct):
                if buy_unit_krw_with_available(unit_krw, krw, price):
                    last_action_ts = time.time()
                time.sleep(CFG.poll_sec); continue

            # 3) 평단 대비 상승 → 전량 매도
            if avg > 0 and price >= avg * (1 + CFG.rise_pct):
                if sell_all_market(coin_bal, price):
                    last_action_ts = time.time()
                time.sleep(CFG.poll_sec); continue

            time.sleep(CFG.poll_sec)

    except KeyboardInterrupt:
        log.info("종료 요청(KeyboardInterrupt)")
    except Exception as e:
        log.exception(f"오류: {e}")
    finally:
        try:
            if fg_worker:
                fg_worker.stop()
        except Exception:
            pass
        ps.stop()
        log.info("정상 종료")

if __name__ == "__main__":
    main()
