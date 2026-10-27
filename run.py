# -*- coding: utf-8 -*-
"""컴비서 실행 진입점. 로컬 서버를 띄우고 브라우저를 엽니다.

v1.5.0: 시작 시 하드웨어를 감지해 자동 최적화하고, 종료 시(창 닫기 포함)
        Ollama 모델을 내려 메모리/VRAM 을 회복합니다.
"""
import os
import socket
import sys
import threading
import webbrowser

import uvicorn

from app import config as cfgmod
from app import diag, perf, sysres
from app.server import app


def free_port(preferred):
    for p in [preferred] + [preferred + i for i in range(1, 30)]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def set_console_title(title):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _arg(name, default=None):
    """--name 값  또는  --name=값  또는  환경변수(COMPIYSO_<NAME>) 를 읽는다."""
    env = os.environ.get("COMPIYSO_" + name.lstrip("-").upper().replace("-", "_"))
    if env:
        return env
    argv = sys.argv
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    for item in argv:
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_console_title(f"{cfgmod.APP_NAME} ({cfgmod.APP_EN}) v{cfgmod.VERSION}")

    # --version : 버전만 출력하고 종료(설치 스크립트/진단용)
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"{cfgmod.APP_NAME} ({cfgmod.APP_EN}) {cfgmod.VERSION}")
        return

    cfg = cfgmod.load_config()

    # 자원 회복 훅(정상 종료 · 창 닫기 · atexit)
    sysres.install_exit_hooks()

    # --cleanup-only : 실행하지 않고 자원만 회복하고 종료
    if "--cleanup-only" in sys.argv:
        rep = sysres.exit_cleanup(cfg)
        print("자원 회복 완료:", rep)
        return

    # 우선순위: 명령줄/환경변수 > 설정값 > 기본값
    #   --host 0.0.0.0  (다른 PC 에서도 UI 접속 허용)
    #   --port 8799
    host = _arg("--host", str(cfg.get("host") or "127.0.0.1"))
    try:
        port = int(_arg("--port", cfg.get("port") or 8799) or 8799)
    except Exception:
        port = 8799
    port = free_port(port)
    url = f"http://{host}:{port}/"
    open_url = f"http://127.0.0.1:{port}/" if host in ("0.0.0.0", "::", "::/0") else url
    # 버전을 URL 에 붙여 브라우저 캐시에 남은 예전 UI 를 쓰지 않게 한다.
    open_url = f"{open_url}?v={cfgmod.VERSION}"

    if "--no-browser" not in sys.argv:
        threading.Timer(1.3, lambda: webbrowser.open(open_url)).start()

    # 하드웨어 감지 + 자동 최적화 결과
    try:
        sysinfo = perf.detect(force=True)
        eff = cfgmod.effective(cfg)
        tier = eff.get("_perf_tier") or sysinfo.get("tier")
        applied = eff.get("_perf_applied") or {}
    except Exception as e:
        diag.log_error("startup.perf", e)
        sysinfo, tier, applied = {}, "balanced", {}

    line = "=" * 60
    print(line)
    print(f"   {cfgmod.APP_NAME} ({cfgmod.APP_EN})  v{cfgmod.VERSION}")
    print(f"   주소: {url}")
    print(f"   모델: {(cfg.get('provider') or 'ollama')} / "
          f"{(cfg.get('openai_model') if cfg.get('provider') == 'openai' else cfg.get('ollama_model'))}"
          f"  ({cfg.get('ollama_url') if cfg.get('provider') != 'openai' else cfg.get('openai_base_url')})")
    print(f"   사양: CPU {sysinfo.get('cpu', '?')}코어 · RAM {sysinfo.get('mem_total_mb', 0)}MB"
          f" · 여유 {sysinfo.get('mem_avail_mb', 0)}MB")
    print(f"   모드: {perf.TIER_LABEL.get(tier, tier)}"
          + (f" (자동 조정 {len(applied)}개)" if applied else ""))
    print(f"   작업 폴더: {cfg.get('workspace')}")
    print(f"   데이터: {cfgmod.data_dir()}")
    print("   종료하면 모델을 내려 메모리/VRAM 을 회복합니다.")
    print(line)

    code = 0
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        code = 1
        diag.log_error("run.uvicorn", e)
        print(f"\n[오류] 서버를 시작하지 못했습니다: {type(e).__name__}: {e}")
    finally:
        try:
            sysres.exit_cleanup(cfg)
        except Exception:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
