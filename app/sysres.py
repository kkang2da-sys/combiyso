# -*- coding: utf-8 -*-
"""시스템 자원 회복 — 종료/요청 시 메모리 · VRAM 반납 (v1.5.0).

프로그램을 닫을 때
- 로컬(또는 원격) Ollama 에 올라가 있는 모델을 **메모리/VRAM 에서 내린다**
  (`keep_alive: 0`). 이게 GPU VRAM 을 가장 크게 회수한다.
- 파이썬 캐시(세션 인덱스, HTTP 연결)를 비우고 gc 를 돌린다.
- 프로세스 **작업 세트(working set)** 를 비워 OS 에 메모리를 돌려준다.

모든 동작은 실패해도 조용히 넘어간다(종료가 막히지 않게).
"""
from __future__ import annotations

import atexit
import os
import threading
import time

from . import llm

_LOCK = threading.Lock()
_DONE = {"exit": False, "last": None}
_CTRL_REF = None          # 콘솔 핸들러 콜백 참조 유지(GC 방지)


# --------------------------------------------------------------------------- #
# 프로세스 메모리
# --------------------------------------------------------------------------- #
def proc_mem_mb():
    """이 프로세스의 실제 사용 메모리(MB). 실패하면 None."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            h = k32.GetCurrentProcess()
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            fn = getattr(k32, "K32GetProcessMemoryInfo", None) or psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            fn.restype = wintypes.BOOL
            if not fn(h, ctypes.byref(pmc), pmc.cb):
                return None
            return int(pmc.WorkingSetSize / 1048576)
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def trim_working_set():
    """gc 후 OS 에 작업 세트를 반납. {before_mb, after_mb, freed_mb}"""
    before = proc_mem_mb()
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            h = k32.GetCurrentProcess()
            k32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE,
                                                    ctypes.c_size_t, ctypes.c_size_t]
            k32.SetProcessWorkingSetSize.restype = wintypes.BOOL
            k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
            try:
                psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
                psapi.EmptyWorkingSet.restype = wintypes.BOOL
                psapi.EmptyWorkingSet(h)
            except Exception:
                pass
        except Exception:
            pass
    after = proc_mem_mb()
    freed = None
    if before is not None and after is not None:
        freed = max(0, before - after)
    return {"before_mb": before, "after_mb": after, "freed_mb": freed}


# --------------------------------------------------------------------------- #
# Ollama 모델 언로드 (VRAM/RAM 회수)
# --------------------------------------------------------------------------- #
def _cfg_urls(cfg, include_remote=None):
    """언로드할 Ollama 서버 주소 목록(중복 제거)."""
    cfg = cfg or {}
    urls = []
    if (cfg.get("provider") or "ollama") != "openai":
        active = llm.ollama_base(cfg)
        if active:
            urls.append(active)
    mode = str(cfg.get("release_on_exit") or "auto").lower()
    if include_remote is None:
        include_remote = (mode == "on")
    if include_remote:
        for s in cfg.get("ollama_servers") or []:
            u = s.get("url") if isinstance(s, dict) else s
            if u:
                urls.append(llm.normalize_url(u))
    out = []
    for u in urls:
        n = llm.normalize_url(u)
        if n not in out:
            out.append(n)
    return out


def unload_models(cfg=None, include_remote=None, urls=None):
    """지정(기본: 현재) Ollama 서버의 올라가 있는 모델을 모두 내린다."""
    cfg = cfg or {}
    targets = list(urls or _cfg_urls(cfg, include_remote))
    report = {"servers": [], "unloaded": [], "freed_mb": 0, "errors": []}
    for url in targets:
        entry = {"url": url, "models": [], "unloaded": [], "freed_mb": 0, "error": ""}
        try:
            running = llm.ollama_ps_url(cfg, url, timeout=3)
            names = [m.get("name") for m in running if m.get("name")]
            entry["models"] = names
            freed = 0
            for m in running:
                name = m.get("name")
                if not name:
                    continue
                ok, err = llm.unload_model(cfg, url, name)
                if ok:
                    entry["unloaded"].append(name)
                    report["unloaded"].append(name)
                    try:
                        freed += int(m.get("size") or 0) / 1048576.0
                    except Exception:
                        pass
                else:
                    report["errors"].append(f"{name}: {err}")
            entry["freed_mb"] = round(freed, 1)
            report["freed_mb"] += entry["freed_mb"]
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            report["errors"].append(f"{url}: {entry['error']}")
        report["servers"].append(entry)
    report["freed_mb"] = round(report["freed_mb"], 1)
    report["ok"] = not report["errors"]
    return report


# --------------------------------------------------------------------------- #
# 캐시 정리 / 통합 cleanup
# --------------------------------------------------------------------------- #
def clear_caches():
    """메모리를 붙잡고 있는 내부 캐시를 비운다."""
    out = {}
    try:
        from . import sessions
        out["session_cache"] = sessions.drop_cache()
    except Exception:
        out["session_cache"] = False
    try:
        from . import net
        net.close_all()
        out["http_pool"] = True
    except Exception:
        out["http_pool"] = False
    try:
        import gc
        out["gc"] = gc.collect()
    except Exception:
        out["gc"] = 0
    return out


def cleanup(cfg=None, unload=True, trim=True, include_remote=None):
    """지금 바로 자원 회복. 결과 보고서(dict) 반환."""
    cfg = cfg or {}
    t0 = time.time()
    before = proc_mem_mb()
    ollama = None
    if unload:
        try:
            ollama = unload_models(cfg, include_remote=include_remote)
        except Exception as e:
            ollama = {"ok": False, "unloaded": [], "freed_mb": 0,
                      "errors": [f"{type(e).__name__}: {e}"]}
    caches = clear_caches()
    triminfo = trim_working_set() if trim else {}
    after = proc_mem_mb()
    report = {
        "ok": True,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "process_before_mb": before if before is not None else triminfo.get("before_mb"),
        "process_after_mb": after if after is not None else triminfo.get("after_mb"),
        "process_freed_mb": triminfo.get("freed_mb"),
        "caches": caches,
        "ollama": ollama,
    }
    _DONE["last"] = report
    return report


def last_report():
    return _DONE["last"]


# --------------------------------------------------------------------------- #
# 종료 훅
# --------------------------------------------------------------------------- #
def exit_cleanup(cfg=None, force=False):
    """프로그램 종료 시 1회만 실행. 실패해도 조용히 넘어간다."""
    with _LOCK:
        if _DONE["exit"] and not force:
            return _DONE["last"]
        _DONE["exit"] = True
    report = None
    try:
        if cfg is None:
            try:
                from . import config
                cfg = config.load_config()
            except Exception:
                cfg = {}
        mode = str((cfg or {}).get("release_on_exit") or "auto").lower()
        unload = mode != "off"
        trim = bool((cfg or {}).get("cleanup_on_exit", True))
        report = cleanup(cfg, unload=unload, trim=trim)
    except Exception:
        report = {"ok": False}
    try:
        from . import mcp
        mcp.MANAGER.stop_all()
    except Exception:
        pass
    try:
        from . import diag
        diag.log_event("exit", "자원 회복: 프로세스 "
                       f"{report.get('process_before_mb')}→{report.get('process_after_mb')}MB, "
                       f"모델 {len((report.get('ollama') or {}).get('unloaded') or [])}개 언로드")
    except Exception:
        pass
    return report


def _console_handler(event):
    """Windows 콘솔 이벤트. CTRL_C(0)/CTRL_BREAK(1) 은 기본 처리에 맡긴다."""
    if event in (2, 5, 6):          # 닫기 / 로그오프 / 시스템 종료
        try:
            exit_cleanup()
        except Exception:
            pass
        return True
    return False


def install_exit_hooks():
    """atexit + (윈도우) 콘솔 닫기 이벤트에 자원 회복을 연결한다."""
    global _CTRL_REF
    try:
        atexit.register(exit_cleanup)
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        _CTRL_REF = proto(_console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_REF, True)
    except Exception:
        pass
