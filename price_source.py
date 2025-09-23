# price_source.py
import threading
import time
from typing import Optional, List
from pyupbit import WebSocketManager

class PriceStream:
    """
    업비트 WebSocket으로 실시간 ticker 가격을 받아
    최신가를 스레드-안전하게 보관하는 경량 스트리머.
    """
    def __init__(self, markets: List[str]):
        self._markets = markets
        self._wm = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_price = {}   # code -> float
        self._last_recv_ts = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="PriceStream", daemon=True)
        self._thread.start()

    def _run(self):
        self._wm = WebSocketManager("ticker", self._markets)
        try:
            while not self._stop.is_set():
                data = self._wm.get()  # 예: {'code':'KRW-BTC','tp':160000000,...}
                code = data.get("code")
                price = data.get("tp") or data.get("trade_price")
                if code and price:
                    with self._lock:
                        self._last_price[code] = float(price)
                        self._last_recv_ts = time.time()
        except Exception:
            pass
        finally:
            try:
                if self._wm:
                    self._wm.terminate()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_last(self, code: str) -> Optional[float]:
        """마지막으로 수신한 가격 반환(없으면 None)."""
        with self._lock:
            return self._last_price.get(code)

    def last_recv_age(self) -> float:
        """마지막 수신 후 경과 초(없으면 매우 큰 값)."""
        with self._lock:
            if self._last_recv_ts == 0:
                return 1e9
            return time.time() - self._last_recv_ts
