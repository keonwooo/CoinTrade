# discord_notify.py
import os
import requests
from typing import Optional, Dict

def _get_webhook_url_for_mode(mode: str, doc: Optional[Dict] = None) -> Optional[str]:
    # 1_ config.yml 우선
    dcfg = ((doc or {}).get("notify") or {}).get("discord") or {}
    hooks = dcfg.get("webhooks") or {}
    url = hooks.get(mode) or dcfg.get("default_webhook")
    if url:
        return url
    env_key = f"DISCORD_WEBHOOK_{mode}".upper()
    url = os.getenv(env_key)
    if url:
        return url
    return os.getenv("DISCORD_WEBHOOK_DEFAULT")

def send_discord(mode: str, text: str, doc: Optional[Dict] = None, *,
                title: Optional[str] = None,
                username: Optional[str] = None,
                avatar_url: Optional[str] = None,
                timeout: float = 5.0) -> bool:
    """
    모드별 채널(웹훅)으로 알림 전송
    - mode: "basic" | "volatility" | "volume" | "rsi" | "rsi_trend"
    - text: 본문
    - doc: config.yml 파싱결과(dict). notify.discord.* 설정 사용
    - title: 임베드 제목(선택)
    - username / avatar_url: 덮어쓰기(선택)
    """
    url = _get_webhook_url_for_mode(mode, doc)
    if not url:
        return False
    
    # config의 기본 username/avatar_url
    dcfg = ((doc or {}).get("notify") or {}).get("discord") or {}
    if username is None:
        username = mode or dcfg.get("username") or "CoinTrade Bot"
    if avatar_url is None:
        avatar_url = dcfg.get("avatar_url") or None

    # 임베드/일반 선택
    if title:
        payload = {
            "username": username,
            "avatar_url": avatar_url,
            "embeds": [{
                "title": title,
                "description": text,
            }]
        }
    else:
        payload = {
            "username": username,
            "avatar_url": avatar_url,
            "content": text
        }

    try:
        r = requests.post(url, json=payload, timeout=timeout)
        return 200 <= r.status_code < 300
    except Exception:
        return False