# -*- coding: utf-8 -*-
"""Ollama 자동 탐색 / 자동 시작 (v1.6.0).

이 PC 와 **내부 네트워크**에 있는 Ollama 를 찾아 모델 목록까지 돌려준다.

- `local_info(start=True)` : 이 PC 의 Ollama 상태 확인 → 꺼져 있으면 자동으로 켠다.
  서버가 아예 안 떠 있어도 `~/.ollama/models/manifests` 를 읽어 **설치된 모델**은
  보여준다(모델이 있는데 "검색 안 됨" 으로 보이던 문제 해결).
- `scan_lan()`      : 이 PC 가 붙은 /24 대역을 병렬 스캔해 11434 포트가 열린 PC/맥을 찾고
  각 서버의 모델 목록·버전·응답시간을 함께 돌려준다.
- `invalidate()`    : 캐시 비우기(설정 화면에서 새로고침할 때).

스캔은 소켓만 쓰고 외부 라이브러리(zeroconf 등)에 의존하지 않는다.
결과는 45초 캐시 → 설정 창을 닫았다 열어도 다시 3초씩 기다리지 않는다.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import llm, perf

PORT = 11434
SCAN_TTL = 45.0          # 네트워크 스캔 결과 캐시(초)
LOCAL_TTL = 10.0         # 이 PC 상태 캐시(초)

_CACHE = {"scan": (0.0, None), "local": (0.0, None)}
_LOCK = threading.Lock()


def invalidate():
    """캐시를 비운다(다음 호출은 실제로 다시 확인)."""
    with _LOCK:
        _CACHE["scan"] = (0.0, None)
        _CACHE["local"] = (0.0, None)


# --------------------------------------------------------------------------- #
# 공통
# --------------------------------------------------------------------------- #
def local_url() -> str:
    return f"http://127.0.0.1:{PORT}"


def port_open(host: str, port: int = PORT, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_exe() -> str:
    """설치된 ollama 실행 파일 경로(없으면 "")."""
    cands = []
    which = shutil.which("ollama")
    if which:
        cands.append(Path(which))
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if base:
            cands.append(Path(base) / "Programs" / "Ollama" / "ollama.exe")
        cands.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe")
    elif sys.platform == "darwin":
        cands += [Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama"),
                  Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    else:
        cands += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"),
                  Path("/snap/bin/ollama"), Path("/opt/ollama/bin/ollama")]
    for p in cands:
        try:
            if p and p.exists():
                return str(p)
        except Exception:
            continue
    return ""


def _spawn(cmd):
    """앱과 분리된 프로세스로 실행(콘솔 창 없이)."""
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "stdin": subprocess.DEVNULL, "close_fds": True}
    if sys.platform.startswith("win"):
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kw["creationflags"] = flags
    return subprocess.Popen(cmd, **kw)


def start_local(wait: float = 20.0):
    """이 PC 의 Ollama 를 켠다. (ok, message, how)"""
    if port_open("127.0.0.1", PORT, 0.4):
        return True, "이미 실행 중입니다.", "running"
    exe = ollama_exe()
    if not exe:
        return (False, "이 PC 에 Ollama 가 설치되어 있지 않습니다. "
                       "https://ollama.com/download 에서 설치한 뒤 다시 시도하세요.", "missing")
    cmd, how = None, ""
    if sys.platform == "darwin" and Path("/Applications/Ollama.app").exists():
        cmd, how = ["open", "-a", "Ollama"], "Ollama 앱"
    elif sys.platform.startswith("win"):
        app = Path(exe).with_name("ollama app.exe")
        if app.exists():
            cmd, how = [str(app)], "Ollama 트레이 앱"
        else:
            cmd, how = [exe, "serve"], "ollama serve"
    else:
        cmd, how = [exe, "serve"], "ollama serve"
    try:
        _spawn(cmd)
    except Exception as e:
        if cmd[0] != exe:
            try:
                _spawn([exe, "serve"])
                how = "ollama serve"
            except Exception as e2:
                return False, f"실행 실패: {type(e2).__name__}: {e2}", "error"
        else:
            return False, f"실행 실패: {type(e).__name__}: {e}", "error"
    t0 = time.time()
    while time.time() - t0 < wait:
        if port_open("127.0.0.1", PORT, 0.4):
            return True, f"{how} 로 Ollama 를 켰습니다 ({time.time() - t0:.1f}초).", "started"
        time.sleep(0.35)
    return (False, f"{how} 를 실행했지만 {wait:.0f}초 안에 응답이 없습니다. "
                   "Ollama 앱을 직접 실행해 보세요.", "timeout")


# --------------------------------------------------------------------------- #
# 설치된 모델(서버가 꺼져 있어도 확인)
# --------------------------------------------------------------------------- #
def model_roots():
    roots = []
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        roots.append(Path(env))
    try:
        roots.append(Path.home() / ".ollama" / "models")
    except Exception:
        pass
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if base:
            roots.append(Path(base) / "Ollama" / "models")
    elif sys.platform == "darwin":
        roots.append(Path("/usr/share/ollama/.ollama/models"))
    out, seen = [], set()
    for r in roots:
        try:
            key = str(r).lower()
            if key in seen or not r.exists():
                continue
            seen.add(key)
            out.append(r)
        except Exception:
            continue
    return out


def installed_models():
    """manifest 폴더에서 모델 이름/크기를 읽는다(서버 불필요)."""
    found = {}
    for root in model_roots():
        base = root / "manifests"
        if not base.exists():
            continue
        try:
            paths = list(base.rglob("*"))
        except Exception:
            continue
        for p in paths:
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(base).parts
                if len(rel) < 3:
                    continue
                ns, model, tag = rel[-3], rel[-2], rel[-1]
                name = f"{model}:{tag}" if ns == "library" else f"{ns}/{model}:{tag}"
                size = 0
                try:
                    data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                    size = sum(int((l or {}).get("size") or 0)
                               for l in (data.get("layers") or []))
                except Exception:
                    pass
                if name in found and not size:
                    continue
                found[name] = {"name": name, "size": size, "params": "",
                               "family": "", "installed": True}
            except Exception:
                continue
    return sorted(found.values(), key=lambda m: m["name"])


# --------------------------------------------------------------------------- #
# 이 PC
# --------------------------------------------------------------------------- #
def local_info(cfg: dict = None, start: bool = False, wait: float = 20.0) -> dict:
    """이 PC 의 Ollama 상태 + 모델. start=True 면 꺼져 있을 때 자동으로 켠다."""
    now = time.time()
    with _LOCK:
        ts, cached = _CACHE["local"]
    if not start and cached and (now - ts) < LOCAL_TTL:
        return cached

    cfg = cfg or {}
    running = port_open("127.0.0.1", PORT, 0.4)
    note, started, how = "", False, ""
    if not running and start:
        ok, msg, how = start_local(wait=wait)
        note = msg
        started = ok
        running = port_open("127.0.0.1", PORT, 0.4)

    models, error = [], ""
    if running:
        try:
            models = llm.ollama_tags(cfg, local_url())
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    installed = installed_models()
    if not running and installed and not note:
        note = (f"Ollama 서버가 꺼져 있어 설치된 모델 {len(installed)}개만 표시합니다. "
                "'이 PC 검색'을 누르면 자동으로 켭니다.")
    if running and not models and installed:
        models = list(installed)      # 태그 조회 실패 시 manifest 로 대체
    models = sorted(models, key=lambda m: str(m.get("name") or ""))

    out = {"ok": bool(running and not error), "url": local_url(), "port": PORT,
           "running": running, "started": started, "how": how,
           "models": models, "installed": installed, "error": error,
           "note": note, "exe": ollama_exe()}
    with _LOCK:
        _CACHE["local"] = (time.time(), out)
    return out


# --------------------------------------------------------------------------- #
# 내부 네트워크 스캔
# --------------------------------------------------------------------------- #
def _local_ips():
    """기본 라우트 IP 를 가장 앞에 두고 이 PC 의 IPv4 주소를 모은다."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    try:
        for ip in perf.lan_addresses():
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def subnets(limit: int = 3):
    """이 PC 가 붙은 /24 대역 목록(예: ['192.168.1'])."""
    out = []
    for ip in _local_ips():
        parts = str(ip).split(".")
        if len(parts) != 4:
            continue
        net = ".".join(parts[:3])
        if net in out or net.startswith("127.") or net.startswith("169.254."):
            continue
        out.append(net)
    try:
        limit = max(1, min(8, int(limit or 3)))
    except Exception:
        limit = 3
    return out[:limit]


def _scan_hosts(hosts, timeout: float = 0.35, workers: int = 128):
    def one(ip):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((ip, PORT)) == 0:
                    return ip
        except Exception:
            return None
        return None

    alive = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(one, hosts, chunksize=8):
                if r:
                    alive.append(r)
    except Exception:
        pass
    return alive


def scan_lan(cfg: dict = None, refresh: bool = False,
             limit_subnets: int = 3, timeout: float = 0.35) -> dict:
    """같은 네트워크의 Ollama 서버를 찾는다.

    반환: {ok, servers:[{url,version,models,latency_ms,local,...}],
           subnets, scanned, found, elapsed_ms}
    """
    now = time.time()
    with _LOCK:
        ts, cached = _CACHE["scan"]
    if not refresh and cached and (now - ts) < SCAN_TTL:
        return cached

    cfg = cfg or {}
    nets = subnets(limit_subnets)
    hosts = [f"{net}.{i}" for net in nets for i in range(1, 255)]
    t0 = time.time()
    found = _scan_hosts(hosts, timeout=timeout) if hosts else []

    urls = []
    if port_open("127.0.0.1", PORT, 0.3):
        urls.append(local_url())
    self_ips = set(_local_ips())          # 같은 PC 를 LAN 주소로 중복 발견하지 않도록
    for ip in found:
        if ip in self_ips:
            continue
        u = f"http://{ip}:{PORT}"
        if u not in urls:
            urls.append(u)

    servers = []
    if urls:
        probe_cfg = {"ollama_connect_timeout": 3}
        try:
            with ThreadPoolExecutor(max_workers=min(12, len(urls))) as ex:
                for info in ex.map(lambda u: llm.probe(probe_cfg, u, timeout=3), urls):
                    if info.get("ok"):
                        servers.append(info)
                    else:
                        servers.append(info)      # 실패도 표시(포트는 열렸는데 응답 없음)
        except Exception:
            servers = []

    servers.sort(key=lambda s: (0 if s.get("local") else 1,
                                s.get("latency_ms") or 99999))
    for s in servers:
        s["models"] = sorted(s.get("models") or [],
                             key=lambda m: str(m.get("name") or ""))
    out = {"ok": True, "servers": servers, "subnets": nets,
           "scanned": len(hosts), "found": sum(1 for s in servers if s.get("ok")),
           "elapsed_ms": int((time.time() - t0) * 1000),
           "hint": llm.HOST_HINT}
    with _LOCK:
        _CACHE["scan"] = (time.time(), out)
    return out
