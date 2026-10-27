# -*- coding: utf-8 -*-
"""가벼운 진단 로그 (v1.5.0).

프로그램이 예외로 죽지 않도록, 오류를 `%APPDATA%\\compbiyso\\logs\\error.log` 에
남기고 계속 동작하게 한다. 로그는 512KB 를 넘으면 자동으로 비운다(디스크 보호).
"""
from __future__ import annotations

import os
import threading
import time
import traceback

from .config import LOGS_DIR

LOG_PATH = LOGS_DIR / "error.log"
MAX_BYTES = 512 * 1024
_LOCK = threading.Lock()


def _rotate():
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_BYTES:
            os.replace(str(LOG_PATH), str(LOG_PATH.with_suffix(".log.old")))
    except Exception:
        try:
            LOG_PATH.unlink()
        except Exception:
            pass


def _write(line: str):
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            _rotate()
            with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
                f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def log_error(scope: str, exc=None, **extra):
    """오류 기록. exc 가 예외면 traceback 도 함께."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"[{ts}] ERROR {scope}"]
    if isinstance(exc, BaseException):
        parts.append(f"{type(exc).__name__}: {exc}")
        try:
            parts.append(traceback.format_exc().strip())
        except Exception:
            pass
    elif exc:
        parts.append(str(exc))
    if extra:
        try:
            parts.append(" ".join(f"{k}={v}" for k, v in extra.items()))
        except Exception:
            pass
    _write(" | ".join(parts))


def log_event(scope: str, msg: str = ""):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(f"[{ts}] INFO {scope} {msg}")


def tail(n: int = 40) -> str:
    """최근 로그 n줄."""
    try:
        if not LOG_PATH.exists():
            return ""
        with _LOCK:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, int(n or 40)):])
    except Exception:
        return ""


def clear() -> bool:
    try:
        with _LOCK:
            if LOG_PATH.exists():
                LOG_PATH.unlink()
            old = LOG_PATH.with_suffix(".log.old")
            if old.exists():
                old.unlink()
        return True
    except Exception:
        return False
