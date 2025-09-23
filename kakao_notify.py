# kakao_notify.py
import json, os, time, requests, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KAKAO_FILE = os.path.join(BASE_DIR, "config", "kakao.json")
KAPI_SEND   = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAUTH_TOKEN = "https://kauth.kakao.com/oauth/token"

_lock = threading.Lock()
_cfg = None
_last_loaded = 0.0

def _load():
    global _cfg, _last_loaded
    with _lock:
        # 10초 캐시
        if not _cfg or time.time() - _last_loaded > 10:
            with open(KAKAO_FILE, "r", encoding="utf-8") as f:
                _cfg = json.load(f)
            _last_loaded = time.time()
    return _cfg

def _save(cfg):
    tmp = KAKAO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, KAKAO_FILE)

def _refresh_token():
    cfg = _load()
    data = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "refresh_token": cfg["refresh_token"],
    }
    r = requests.post(KAUTH_TOKEN, data=data, timeout=8)
    r.raise_for_status()
    j = r.json()
    if "access_token" in j:
        cfg["access_token"] = j["access_token"]
    if "refresh_token" in j:  # 드물게 새로 내려올 때만 갱신
        cfg["refresh_token"] = j["refresh_token"]
    _save(cfg)
    return cfg

def send_kakao_text(text: str) -> bool:
    """
    카카오톡 '나에게 보내기' (기본 템플릿) 전송.
    """
    cfg = _load()
    if not cfg.get("enabled"):
        return False

    headers = {"Authorization": f"Bearer {cfg['access_token']}"}
    template_object = {
        "object_type": "text",
        "text": text[:990],          # 정책상 길이 제한 대비
        "link": {"web_url": "https://developers.kakao.com"}
    }
    resp = requests.post(
        KAPI_SEND,
        headers=headers,
        data={"template_object": json.dumps(template_object, ensure_ascii=False)},
        timeout=8,
    )

    if resp.status_code == 401:
        # 토큰 만료 → 리프레시 후 1회 재시도
        cfg = _refresh_token()
        headers = {"Authorization": f"Bearer {cfg['access_token']}"}
        resp = requests.post(
            KAPI_SEND,
            headers=headers,
            data={"template_object": json.dumps(template_object, ensure_ascii=False)},
            timeout=8,
        )
    return resp.ok
