# -*- coding: utf-8 -*-
"""공용 HTTP 연결 풀 (스레드별).

requests.Session 은 스레드 안전이 아니므로 **스레드마다 하나씩** 만든다.
(병렬 하위 에이전트가 동시에 LLM 을 호출할 때 필요하다. urllib3 의
커넥션 풀 자체는 스레드 안전하므로 어댑터는 공유한다.)

v1.5.0: `close_all()` — 프로그램 종료/자원 회복 때 연결을 닫아 소켓·버퍼를 반납.
"""
import threading

import requests

_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=8, pool_maxsize=16, max_retries=0)
_local = threading.local()
_SESSIONS = []
_LOCK = threading.Lock()


def _make():
    s = requests.Session()
    s.mount("http://", _ADAPTER)
    s.mount("https://", _ADAPTER)
    with _LOCK:
        _SESSIONS.append(s)
        if len(_SESSIONS) > 32:          # 오래된 스레드 세션 정리
            _SESSIONS.pop(0)
    return s


def session():
    """현재 스레드 전용 세션(연결 재사용)."""
    s = getattr(_local, "s", None)
    if s is None:
        s = _make()
        _local.s = s
    return s


def close_all():
    """모든 세션의 연결을 닫는다(메모리/소켓 반납)."""
    n = 0
    with _LOCK:
        items = list(_SESSIONS)
        _SESSIONS.clear()
    for s in items:
        try:
            s.close()
            n += 1
        except Exception:
            pass
    try:
        _local.s = None
    except Exception:
        pass
    return n


SESSION = _make()        # 하위 호환(단일 스레드 경로)
