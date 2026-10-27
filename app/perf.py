# -*- coding: utf-8 -*-
"""자동 성능 최적화 · 자원 감시 (v1.5.0).

목표
- **저사양 PC 에서도 시스템 에러 없이** 돌아간다.
- 하드웨어(메모리·CPU·GPU/VRAM)를 감지해 등급(lite/low/balanced/full)을 정하고,
  등급에 맞춰 컨텍스트·도구 출력·병렬 실행 등을 **자동으로 보수적으로** 낮춘다.
- 사용자가 기본값에서 **직접 바꾼 항목은 절대 건드리지 않는다**.
- 실행 중 메모리 압박을 감시해(runtime governor) 더 조인다. 부족해도 죽지 않는다.

설계 원칙
- 외부 의존성 없음(psutil 불필요) — ctypes / os / subprocess 만 사용
- 모든 함수는 예외를 삼키고 **항상 쓸 수 있는 값**을 돌려준다
- 시작 속도를 위해 GPU 조회는 캐시 + 백그라운드 1회
"""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

TIERS = ("lite", "low", "balanced", "full")
TIER_LABEL = {"lite": "아주 낮음", "low": "낮음", "balanced": "보통", "full": "넉넉함"}
TIER_DESC = {
    "lite": "아주 저사양 — 최소 메모리로 동작하도록 강하게 조입니다.",
    "low": "저사양 — 출력·컨텍스트·병렬 실행을 줄여 자원을 아낍니다.",
    "balanced": "보통 — 기본 설정으로 동작합니다.",
    "full": "넉넉함 — 아무 제한 없이 최대 성능으로 동작합니다.",
}

# 등급별 자동 적용 값. (사용자가 기본값에서 바꾼 항목은 적용하지 않는다)
PROFILES = {
    "lite": {
        "compact_threshold": 3000,
        "max_tool_output": 4000,
        "keep_recent_messages": 4,
        "max_steps": 18,
        "subagent_max_steps": 8,
        "ollama_num_ctx": 2048,
        "ollama_num_predict": 512,
        "ollama_keep_alive": "5m",
        "subagent_parallel": "off",
        "image_max_side": 768,
        "pdf_max_pages": 10,
        "backup_keep": 120,
    },
    "low": {
        "compact_threshold": 5000,
        "max_tool_output": 6000,
        "keep_recent_messages": 6,
        "max_steps": 25,
        "subagent_max_steps": 12,
        "ollama_num_ctx": 4096,
        "ollama_num_predict": 1024,
        "ollama_keep_alive": "10m",
        "subagent_parallel": "off",
        "image_max_side": 1024,
        "pdf_max_pages": 20,
        "backup_keep": 200,
    },
    "balanced": {},
    "full": {},
}

# 메모리 압박 단계(0 정상 ~ 3 위험)
PRESSURE_LABEL = {0: "정상", 1: "주의", 2: "부족", 3: "위험"}


# --------------------------------------------------------------------------- #
# 저수준 감지 (모두 실패해도 안전)
# --------------------------------------------------------------------------- #
def cpu_count() -> int:
    try:
        return max(1, int(os.cpu_count() or 1))
    except Exception:
        return 1


def mem_info():
    """(전체 MB, 사용가능 MB). 실패하면 (0, 0)."""
    try:
        if os.name == "nt":
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MS()
            st.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return (int(st.ullTotalPhys / 1048576), int(st.ullAvailPhys / 1048576))
        total = avail = 0
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) // 1024
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) // 1024
        except Exception:
            pass
        if not total:
            try:
                import shutil
                total = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                            / 1048576)
            except Exception:
                pass
        return total, avail
    except Exception:
        return 0, 0


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_GPU_CACHE = {"ts": 0.0, "data": None, "busy": False}
_GPU_LOCK = threading.Lock()


def _run_quiet(cmd, timeout=4):
    """콘솔 창 없이 명령 실행 → stdout 문자열(실패하면 '')."""
    try:
        kw = {"capture_output": True, "timeout": timeout}
        if os.name == "nt":
            kw["creationflags"] = _NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
        r = subprocess.run(cmd, **kw)
        return (r.stdout or b"").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _nvidia_smi():
    exe_candidates = ["nvidia-smi"]
    if os.name == "nt":
        exe_candidates.append(str(os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")))
    for exe in exe_candidates:
        out = _run_quiet([exe, "--query-gpu=name,memory.total,memory.used",
                          "--format=csv,noheader,nounits"], timeout=4)
        if not out:
            continue
        rows = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                rows.append({"name": parts[0],
                             "vram_total_mb": int(float(parts[1])),
                             "vram_used_mb": int(float(parts[2]))})
            except Exception:
                continue
        if rows:
            return {"known": True, "ok": True, "gpus": rows,
                    "name": rows[0]["name"],
                    "vram_total_mb": sum(r["vram_total_mb"] for r in rows),
                    "vram_used_mb": sum(r["vram_used_mb"] for r in rows)}
    return None


def gpu_info(wait=False, ttl=20.0):
    """GPU/VRAM 정보(캐시). wait=False 면 처음엔 '감지 중'을 즉시 돌려준다."""
    now = time.time()
    with _GPU_LOCK:
        data = _GPU_CACHE["data"]
        fresh = data is not None and (now - _GPU_CACHE["ts"]) < ttl
        if fresh:
            return data
        if _GPU_CACHE["busy"]:
            return data or {"known": False, "ok": False, "busy": True,
                            "note": "GPU 확인 중"}
        _GPU_CACHE["busy"] = True

    def worker():
        try:
            info = _nvidia_smi() or {"known": False, "ok": False,
                                     "note": "NVIDIA GPU 를 찾지 못했습니다."}
        except Exception as e:
            info = {"known": False, "ok": False, "note": f"{type(e).__name__}: {e}"}
        with _GPU_LOCK:
            _GPU_CACHE["data"] = info
            _GPU_CACHE["ts"] = time.time()
            _GPU_CACHE["busy"] = False

    if wait:
        worker()
        with _GPU_LOCK:
            return _GPU_CACHE["data"]
    threading.Thread(target=worker, daemon=True).start()
    return data or {"known": False, "ok": False, "busy": True, "note": "GPU 확인 중"}


def lan_addresses():
    """다른 PC 가 접속할 때 쓸 이 PC 의 IPv4 주소 목록."""
    out = []
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                out.append(ip)
    except Exception:
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                out.append(ip)
        except Exception:
            pass
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# 등급 판정
# --------------------------------------------------------------------------- #
def classify(total_mb: int, cores: int = 0, avail_mb: int = 0) -> str:
    """(전체 RAM, 코어 수) 로 등급 결정. 알 수 없으면 balanced."""
    try:
        total_mb = int(total_mb or 0)
        cores = int(cores or 0)
    except Exception:
        return "balanced"
    if total_mb <= 0:
        return "balanced"
    if total_mb < 4096:
        return "lite"
    if cores and cores <= 2 and total_mb < 8192:
        return "lite"
    if total_mb < 8192:
        return "low"
    if total_mb < 16384:
        return "balanced"
    return "full"


_SYS_CACHE = {"ts": 0.0, "data": None}
_SYS_LOCK = threading.Lock()


def detect(force: bool = False, ttl: float = 5.0) -> dict:
    """현재 시스템 정보(짧게 캐시). GPU 는 캐시값만 넣는다(느리므로)."""
    now = time.time()
    with _SYS_LOCK:
        if not force and _SYS_CACHE["data"] and (now - _SYS_CACHE["ts"]) < ttl:
            return dict(_SYS_CACHE["data"])
    total, avail = mem_info()
    cores = cpu_count()
    tier = classify(total, cores, avail)
    gpu = gpu_info(wait=False)
    data = {
        "tier": tier,
        "tier_label": TIER_LABEL.get(tier, tier),
        "tier_desc": TIER_DESC.get(tier, ""),
        "cpu": cores,
        "mem_total_mb": total,
        "mem_avail_mb": avail,
        "mem_percent": int((total - avail) * 100 / total) if total else 0,
        "gpu": gpu,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "ts": now,
    }
    with _SYS_LOCK:
        _SYS_CACHE["data"] = data
        _SYS_CACHE["ts"] = now
    return dict(data)


def resolve_tier(cfg: dict) -> str:
    """설정(low_spec / auto_optimize)을 반영한 최종 등급."""
    cfg = cfg or {}
    mode = str(cfg.get("low_spec") or "auto").lower()
    if mode == "off":
        return "full"
    if mode == "on":
        # 저사양 모드를 직접 켠 경우에는 자동 최적화 스위치와 무관하게 적용한다.
        base = detect()["tier"]
        return "lite" if base == "lite" else "low"
    if cfg.get("auto_optimize") is False:
        return "full"
    return detect()["tier"]


def adapt(cfg: dict, defaults: dict) -> dict:
    """등급에 맞춰 자원 관련 설정을 낮춘 **사본**을 돌려준다.

    - 사용자가 기본값에서 바꾼 값은 그대로 둔다.
    - `_perf_tier`, `_perf_applied`, `_low_spec_active` 를 붙여 UI 가 알 수 있게 한다.
    """
    out = dict(cfg or {})
    tier = resolve_tier(out)
    applied = {}
    for k, v in (PROFILES.get(tier) or {}).items():
        cur = out.get(k)
        if cur is None or cur == defaults.get(k):
            out[k] = v
            applied[k] = v
    out["_perf_tier"] = tier
    out["_perf_applied"] = applied
    out["_low_spec_active"] = tier in ("lite", "low")
    if tier == "lite":
        out["_force_sequential"] = True
    return out


# --------------------------------------------------------------------------- #
# 런타임 거버너 (메모리 압박 감시)
# --------------------------------------------------------------------------- #
def pressure_level(total_mb: int, avail_mb: int) -> int:
    try:
        total_mb, avail_mb = int(total_mb or 0), int(avail_mb or 0)
    except Exception:
        return 0
    if total_mb <= 0 or avail_mb <= 0:
        return 0
    frac = avail_mb / float(total_mb)
    if avail_mb < 300:
        return 3
    if avail_mb < 600 or frac < 0.06:
        return 2
    if avail_mb < 1200 or frac < 0.12:
        return 1
    return 0


class Governor:
    """2초에 한 번만 실제 메모리를 읽는 가벼운 감시자."""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._state = {"pressure": 0, "avail_mb": 0, "total_mb": 0,
                       "percent": 0, "ts": 0.0, "note": ""}

    def update(self, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            if not force and (now - self._last) < self.interval:
                return dict(self._state)
        total, avail = mem_info()
        p = pressure_level(total, avail)
        state = {
            "pressure": p,
            "pressure_label": PRESSURE_LABEL.get(p, ""),
            "avail_mb": avail,
            "total_mb": total,
            "percent": int((total - avail) * 100 / total) if total else 0,
            "ts": now,
            "note": {
                0: "",
                1: "메모리가 다소 부족합니다. 큰 작업은 느려질 수 있습니다.",
                2: "메모리가 부족해 도구 출력·컨텍스트를 자동으로 줄입니다.",
                3: "메모리가 위험 수준입니다. 대화를 새로 시작하거나 모델을 내려 주세요.",
            }.get(p, ""),
        }
        with self._lock:
            self._state = state
            self._last = now
        return dict(state)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)


GOV = Governor()


def tune(cfg: dict) -> dict:
    """실행 중 메모리 압박에 맞춰 자원 관련 값만 더 낮춘 사본."""
    out = dict(cfg or {})
    if not out.get("memory_guard", True):
        out["_pressure"] = 0
        return out
    snap = GOV.update()
    p = int(snap.get("pressure") or 0)
    out["_pressure"] = p
    out["_mem_avail_mb"] = snap.get("avail_mb")
    if p <= 0:
        return out

    def tight(key, cap):
        try:
            cur = int(out.get(key) or 0)
        except Exception:
            cur = 0
        out[key] = cap if cur <= 0 else min(cur, cap)

    if p >= 1:
        tight("max_tool_output", 8000)
        tight("compact_threshold", 6000)
        tight("keep_recent_messages", 6)
    if p >= 2:
        tight("max_tool_output", 5000)
        tight("compact_threshold", 4000)
        tight("keep_recent_messages", 4)
        tight("ollama_num_ctx", 3072)
        out["_force_sequential"] = True
    if p >= 3:
        tight("max_tool_output", 4000)
        tight("compact_threshold", 3000)
        tight("keep_recent_messages", 3)
        tight("ollama_num_ctx", 2048)
        tight("max_steps", 12)
        out["_force_sequential"] = True
    return out


# --------------------------------------------------------------------------- #
# URL 정규화 (원격 Ollama 주소)
# --------------------------------------------------------------------------- #
def normalize_url(url, default_port: int = 11434) -> str:
    """'192.168.0.5', 'host:11434', 'http://host:11434/' → 표준 URL."""
    s = str(url or "").strip()
    if not s:
        return f"http://127.0.0.1:{default_port}"
    if "://" not in s:
        s = "http://" + s
    try:
        p = urlsplit(s)
        host = p.hostname or "127.0.0.1"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        try:
            port = p.port
        except Exception:
            port = None
        path = p.path or ""
        if port is None and (not path or path == "/"):
            port = default_port
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"          # IPv6
        netloc = host if port is None else f"{host}:{port}"
        if p.username:
            auth = p.username + ((":" + p.password) if p.password else "")
            netloc = auth + "@" + netloc
        return urlunsplit((p.scheme or "http", netloc, path.rstrip("/"), "", ""))
    except Exception:
        return s.rstrip("/")


def is_local_url(url: str) -> bool:
    try:
        host = urlsplit(normalize_url(url)).hostname or ""
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


# --------------------------------------------------------------------------- #
# 안전한 리소스 회수 (메모리 관련 보조)
# --------------------------------------------------------------------------- #
def safe_gc():
    """gc.collect() 를 안전하게. 수거된 객체 수 반환."""
    try:
        import gc
        return gc.collect()
    except Exception:
        return 0


def human_mb(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    if n >= 1024:
        return f"{n / 1024:.1f}GB"
    return f"{n:.0f}MB"
