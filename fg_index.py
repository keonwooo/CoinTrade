# fg_index.py  (CNN FGI fetcher: daily 09:00 KST schedule + de-dupe + cache-bust)
import os
import time
import json
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import requests

# KST 타임존
from zoneinfo import ZoneInfo  # Py3.9+
KST = ZoneInfo("Asia/Seoul")

class FearGreedWorker:
    BASE = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

    def __init__(
        self,
        logs_dir: str,
        log_file: str = "fg_index.log",
        interval_sec: int = 3600,     # mode="interval"일 때만 사용(하위호환)
        notify_kakao: bool = False,
        logger=None,
        start_date: Optional[str] = None,
        notify_on_start: bool = True,
        mode: str = "daily",          # "daily" or "interval"
        daily_kst_hour: int = 9,      # mode="daily"일 때 사용
    ):
        self.logs_dir = logs_dir
        self.log_path = os.path.join(logs_dir, log_file)
        self.state_path = os.path.join(logs_dir, "fg_index_state.json")
        self.interval = max(300, int(interval_sec))
        self.notify_kakao = notify_kakao
        self.notify_on_start = notify_on_start
        self.logger = logger
        self.start_date = start_date
        self.mode = mode
        self.daily_kst_hour = max(0, min(23, int(daily_kst_hour)))

        os.makedirs(self.logs_dir, exist_ok=True)

        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Accept": "application/json, text/plain, */*",
            "Connection": "keep-alive",
        }

        st = self._load_state()
        self._last_ts = st.get("last_ts")
        self._last_val = st.get("last_val")
        self._boot_sent = bool(st.get("boot_sent", False))

        self._stop = threading.Event()
        self._thread = None

    # ---------- 상태 ----------
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_state(self):
        data = {"last_ts": self._last_ts, "last_val": self._last_val, "boot_sent": self._boot_sent}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ---------- 스케줄 ----------
    @staticmethod
    def _now_kst() -> datetime:
        return datetime.now(tz=KST)

    def _next_kst_hour(self, hour: int, after: Optional[datetime] = None) -> datetime:
        base = (after or self._now_kst()).astimezone(KST)
        target = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        return target if base < target else (target + timedelta(days=1))

    # ---------- 수명주기 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="FearGreedWorker", daemon=True)
        self._thread.start()
        if self.logger:
            mode_txt = f"daily@{self.daily_kst_hour:02d}KST" if self.mode == "daily" else f"interval={self.interval}s"
            self.logger.info(f"[FGI] CNN 워커 시작 ({mode_txt}, notify_kakao={self.notify_kakao})")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self.logger:
            self.logger.info("[FGI] CNN 워커 종료")

    # ---------- 메인 루프 ----------
    def _run(self):
        if self.mode == "daily":
            next_ts = self._next_kst_hour(self.daily_kst_hour).timestamp()
            # 시작 즉시 알림 옵션
            if self.notify_on_start and not self._boot_sent:
                self._process_once(force_notify=True)
                self._boot_sent = True
                self._save_state()
            while not self._stop.is_set():
                now = time.time()
                if now >= next_ts:
                    self._process_once()
                    next_dt = self._next_kst_hour(self.daily_kst_hour)
                    next_ts = next_dt.timestamp()
                time.sleep(1)
        else:
            # interval 모드(하위호환)
            if self.notify_on_start and not self._boot_sent:
                self._process_once(force_notify=True)
                self._boot_sent = True
                self._save_state()
            while not self._stop.is_set():
                self._process_once()
                for _ in range(self.interval):
                    if self._stop.is_set():
                        break
                    time.sleep(1)

    # ---------- 1회 처리 ----------
    def _process_once(self, force_notify: bool = False):
        try:
            data = self.fetch_cnn()
            if not data:
                if self.logger:
                    self.logger.warning("[FGI] CNN 응답 파싱 실패")
                return

            changed = self._is_changed(data)
            should_notify = force_notify or changed

            if changed:
                self.write_log_line(data)
                self._last_ts = data.get("timestamp") or self._last_ts
                self._last_val = data.get("value", self._last_val)
                self._save_state()
            else:
                if self.logger:
                    self.logger.info("[FGI] 변화 없음 (동일 데이터)")

            if self.notify_kakao and should_notify:
                try:
                    from kakao_notify import send_kakao_text
                    send_kakao_text(self.format_kakao_msg(data))
                except Exception as e:
                    if self.logger:
                        self.logger.exception(f"[FGI] 카카오 전송 실패: {e}")
        except Exception as e:
            if self.logger:
                self.logger.exception(f"[FGI] 처리 중 오류: {e}")

    def _is_changed(self, d: Dict[str, Any]) -> bool:
        cur_ts = d.get("timestamp")
        cur_val = d.get("value")
        if cur_ts and self._last_ts and cur_ts != self._last_ts:
            return True
        if cur_val is not None and self._last_val is not None and cur_val != self._last_val:
            return True
        return (self._last_ts is None) or (self._last_val is None)

    # ---------- 데이터 수집 ----------
    def fetch_cnn(self) -> Optional[Dict[str, Any]]:
        url = self.BASE if not self.start_date else f"{self.BASE}/{self.start_date}"
        params = {"_": int(time.time())}  # 캐시 무력화
        r = requests.get(url, headers=self._headers, params=params, timeout=10)
        r.raise_for_status()
        j = r.json() or {}

        today = j.get("fear_and_greed") or {}
        score = today.get("score")
        rating = today.get("rating") or today.get("classification") or ""

        # timestamp 후보
        ts_candidates = [
            today.get("timestamp"),
            today.get("timestamp_correction"),
            today.get("last_updated"),
            today.get("lastUpdated"),
            today.get("lastUpdate"),
            (j.get("metadata") or {}).get("lastUpdated"),
        ]
        ts = None
        for cand in ts_candidates:
            ts = self._parse_ts(cand)
            if ts:
                break

        prev = today.get("previous_close") or today.get("previousClose")

        if (score is None or ts is None) and "fear_and_greed_historical" in j:
            hist = (j.get("fear_and_greed_historical") or {}).get("data") or []
            if hist:
                last = hist[-1]
                if score is None:
                    score = last.get("y")
                if ts is None:
                    ts = self._parse_ts(last.get("x"))

        if score is None:
            return None

        try:
            score = int(score)
        except Exception:
            pass

        return {
            "value": score,
            "classification": str(rating).title() if rating else "",
            "timestamp": ts,
            "previous_close": prev,
        }

    # ---------- 출력 ----------
    def write_log_line(self, d: Dict[str, Any]):
        ts_str = "-"
        if d.get("timestamp"):
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(d["timestamp"])))
        line = (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | value={d['value']} "
            f"| class={d.get('classification','')} | ts={ts_str} "
            f"| prev={d.get('previous_close','-')}\n"
        )
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)
        if self.logger:
            self.logger.info(f"[FGI] {line.strip()}")

    def format_kakao_msg(self, d: Dict[str, Any]) -> str:
        ts_str = "-"
        if d.get("timestamp"):
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(d["timestamp"])))
        cls = d.get("classification", "")
        return (
            "[CNN Fear & Greed]\n"
            f"지수: {d['value']}{f' ({cls})' if cls else ''}\n"
            f"데이터시각: {ts_str}\n"
            "출처: edition.cnn.com"
        )

    # ---------- 유틸 ----------
    @staticmethod
    def _parse_ts(ts_val):
        if ts_val is None:
            return None
        # 숫자/숫자문자열
        try:
            ival = int(str(ts_val).strip())
            if ival > 10_000_000_000:  # ms → s
                return ival // 1000
            return ival
        except Exception:
            pass
        # ISO 간단 파싱
        s = str(ts_val).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                return int(datetime.strptime(s, fmt).timestamp())
            except Exception:
                continue
        return None
