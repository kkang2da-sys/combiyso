# -*- coding: utf-8 -*-
"""v1.5.0 신규 기능 테스트 (서버 없이 in-process).

  - 하드웨어 감지/등급 판정 + 자동 최적화(perf)
  - 런타임 메모리 거버너(압박 시 자동 감축)
  - 원격 Ollama 주소 정규화 · 연결 확인 · 모델 언로드
  - 종료/요청 시 자원 회복(sysres)
  - 설정 보정(sanitize) / 마이그레이션
  - UI 스크립트 문법(node --check, 있으면)

실행:  .venv\\Scripts\\python.exe dev\\test_v5.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 데이터 폴더를 임시로 (실제 %APPDATA%\compbiyso 를 건드리지 않는다) ──
_TMPDATA = Path(tempfile.mkdtemp(prefix="cb_v5_"))
os.environ["APPDATA"] = str(_TMPDATA)

from app import config                     # noqa: E402
from app import diag                       # noqa: E402
from app import llm as llmmod              # noqa: E402
from app import perf                       # noqa: E402
from app import sysres                     # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}  {detail}")


# --------------------------------------------------------------------------- #
# 1) 등급 판정
# --------------------------------------------------------------------------- #
print("\n[1] 하드웨어 등급 판정")
check("2GB → lite", perf.classify(2048, 2) == "lite")
check("6GB → low", perf.classify(6000, 4) == "low")
check("6GB·2코어 → lite", perf.classify(6000, 2) == "lite")
check("12GB → balanced", perf.classify(12000, 8) == "balanced")
check("32GB → full", perf.classify(32000, 16) == "full")
check("알 수 없음 → balanced", perf.classify(0, 0) == "balanced")

# --------------------------------------------------------------------------- #
# 2) 주소 정규화
# --------------------------------------------------------------------------- #
print("\n[2] 원격 Ollama 주소 정규화")
cases = {
    "192.168.0.5": "http://192.168.0.5:11434",
    "192.168.0.5:11434": "http://192.168.0.5:11434",
    "http://192.168.0.5:11434/": "http://192.168.0.5:11434",
    "localhost": "http://localhost:11434",
    "https://llm.example.com/v1": "https://llm.example.com/v1",
    "0.0.0.0:11434": "http://127.0.0.1:11434",
    "[::1]:11434": "http://[::1]:11434",
    "": "http://127.0.0.1:11434",
}
for raw, want in cases.items():
    got = perf.normalize_url(raw)
    check(f"{raw!r} → {want}", got == want, f"(got {got})")
check("로컬 판정", perf.is_local_url("127.0.0.1:11434") and not perf.is_local_url("10.0.0.2"))
check("llm.normalize_url 위임", llmmod.normalize_url("10.0.0.9") == "http://10.0.0.9:11434")

# --------------------------------------------------------------------------- #
# 3) 자동 최적화 (사용자 설정 존중)
# --------------------------------------------------------------------------- #
print("\n[3] 자동 최적화")
d = dict(config.DEFAULTS)
d["low_spec"] = "on"
eff = config.effective(d)
check("저사양 모드 적용", eff["_low_spec_active"] is True)
check("max_tool_output 자동 감축", eff["max_tool_output"] == perf.PROFILES["low"]["max_tool_output"])
check("num_ctx 자동 감축", eff["ollama_num_ctx"] == perf.PROFILES["low"]["ollama_num_ctx"])
check("keep_alive 자동 단축", eff["ollama_keep_alive"] == "10m")
check("병렬 자동 끔", eff["subagent_parallel"] == "off")

d2 = dict(config.DEFAULTS)
d2["low_spec"] = "on"
d2["max_tool_output"] = 12345          # 사용자가 직접 바꾼 값
eff2 = config.effective(d2)
check("사용자 값은 유지", eff2["max_tool_output"] == 12345)
check("나머지는 자동", eff2["compact_threshold"] == perf.PROFILES["low"]["compact_threshold"])

d3 = dict(config.DEFAULTS)
d3["low_spec"] = "off"
check("끄면 최대 성능", config.effective(d3)["_perf_tier"] == "full")
check("is_low_spec off", config.is_low_spec(d3) is False)

d4 = dict(config.DEFAULTS)
d4["low_spec"] = "on"
d4["auto_optimize"] = False
check("수동 저사양은 자동 최적화를 꺼도 적용",
      config.effective(d4)["_low_spec_active"] is True)
d5 = dict(config.DEFAULTS)
d5["auto_optimize"] = False
check("자동 최적화만 끄면 최대 성능", config.effective(d5)["_perf_tier"] == "full")

# --------------------------------------------------------------------------- #
# 4) 런타임 메모리 거버너
# --------------------------------------------------------------------------- #
print("\n[4] 메모리 거버너")
check("정상", perf.pressure_level(16000, 8000) == 0)
check("주의", perf.pressure_level(16000, 1500) == 1)
check("부족", perf.pressure_level(16000, 500) == 2)
check("위험", perf.pressure_level(16000, 200) == 3)

_orig_mem = perf.mem_info
try:
    perf.mem_info = lambda: (16000, 250)
    perf.GOV.update(force=True)
    tuned = perf.tune({"memory_guard": True, "max_tool_output": 20000,
                       "compact_threshold": 24000, "keep_recent_messages": 8,
                       "ollama_num_ctx": 8192, "max_steps": 40})
    check("위험 시 출력 4000", tuned["max_tool_output"] == 4000)
    check("위험 시 컨텍스트 3000", tuned["compact_threshold"] == 3000)
    check("위험 시 병렬 금지", tuned.get("_force_sequential") is True)
    check("위험 시 단계 12", tuned["max_steps"] == 12)
    check("압박 단계 노출", tuned["_pressure"] == 3)

    perf.mem_info = lambda: (16000, 8000)
    perf.GOV.update(force=True)
    tuned2 = perf.tune({"memory_guard": True, "max_tool_output": 30000})
    check("정상이면 그대로", tuned2["max_tool_output"] == 30000)
    tuned3 = perf.tune({"memory_guard": False, "max_tool_output": 30000})
    check("가드 끄면 그대로", tuned3["max_tool_output"] == 30000)
finally:
    perf.mem_info = _orig_mem
    perf.GOV.update(force=True)

# --------------------------------------------------------------------------- #
# 5) 가짜 Ollama 서버로 연결/목록/언로드
# --------------------------------------------------------------------------- #
print("\n[5] 원격 Ollama (가짜 서버)")
state = {"unloaded": [], "deleted": []}


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/version"):
            return self._send({"version": "9.9.9"})
        if self.path.startswith("/api/tags"):
            return self._send({"models": [
                {"name": "tiny:1b", "size": 123456789,
                 "details": {"parameter_size": "1B", "family": "llama"}}]})
        if self.path.startswith("/api/ps"):
            return self._send({"models": [
                {"name": "tiny:1b", "size": 104857600, "size_vram": 52428800}]})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if self.path.startswith("/api/generate"):
            state["unloaded"].append(body.get("model"))
            return self._send({"done": True, "response": ""})
        if self.path.startswith("/api/pull"):
            return self._send({"status": "success"})
        return self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            state["deleted"].append(json.loads(raw or b"{}").get("name"))
        except Exception:
            pass
        return self._send({"done": True})


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{port}"

try:
    pr = llmmod.probe({}, url)
    check("probe 성공", pr["ok"] is True and pr["count"] == 1, pr.get("error"))
    check("probe 버전", pr["version"] == "9.9.9")
    check("probe 모델명", pr["models"][0]["name"] == "tiny:1b")
    check("모델 목록", llmmod.list_models({}, url) == ["tiny:1b"])
    ok, err = llmmod.unload_model({}, url, "tiny:1b")
    check("모델 언로드", ok is True and state["unloaded"][-1] == "tiny:1b", err)
    rep = sysres.unload_models({}, urls=[url])
    check("전체 언로드 보고", rep["ok"] and "tiny:1b" in rep["unloaded"])
    check("해방량 계산", rep["freed_mb"] == 100.0, rep["freed_mb"])
    llmmod.ollama_delete({}, "tiny:1b", url)
    check("모델 삭제", state["deleted"][-1] == "tiny:1b")

    # 압박/정상과 무관하게 cleanup 은 실패하지 않아야 한다
    cr = sysres.cleanup({"release_on_exit": "off"}, unload=False)
    check("cleanup 안전", cr["ok"] is True and "caches" in cr)

    # 끊긴 주소는 빠르게 실패하고 힌트를 준다
    llmmod._connect_timeout = lambda cfg: 0.5
    bad = llmmod.probe({}, "127.0.0.1:9")
    check("연결 실패 처리", bad["ok"] is False and "서버" in bad["error"])
finally:
    srv.shutdown()

# --------------------------------------------------------------------------- #
# 6) 설정 보정 / 마이그레이션
# --------------------------------------------------------------------------- #
print("\n[6] 설정 보정 / 마이그레이션")
s = config.sanitize({
    "max_steps": "abc", "temperature": 99, "low_spec": "??",
    "ollama_url": "10.0.0.2", "memory_guard": 0,
    "ollama_servers": [{"url": "10.0.0.3"}, "10.0.0.4", {"url": ""}],
    "permission_rules": "nope",
})
check("잘못된 int → 기본값", s["max_steps"] == config.DEFAULTS["max_steps"])
check("범위 초과 float 보정", s["temperature"] == 2.0)
check("잘못된 enum → 기본값", s["low_spec"] == "auto")
check("주소 정규화", s["ollama_url"] == "http://10.0.0.2:11434")
check("bool 보정", s["memory_guard"] is False)
check("목록 보정", isinstance(s["permission_rules"], list))
check("서버 문자열 마이그레이션", len(s["ollama_servers"]) == 2, s["ollama_servers"])
check("빈 서버 주소 제외", all(x["url"] for x in s["ollama_servers"]))
check("문자열 false → False", config.sanitize({"auto_optimize": "false"})["auto_optimize"] is False)
check("문자열 ON → on", config.sanitize({"low_spec": " ON "})["low_spec"] == "on")

m = config._migrate({"low_spec": True, "ollama_servers": ["10.0.0.7"]},
                    dict(config.DEFAULTS, low_spec=True,
                         ollama_servers=["10.0.0.7"],
                         ollama_url="10.0.0.8"))
check("bool low_spec 마이그레이션", m["low_spec"] == "on")
check("서버 목록 dict 변환", m["ollama_servers"][0]["url"] == "http://10.0.0.7:11434")
check("ollama_url 마이그레이션", m["ollama_url"] == "http://10.0.0.8:11434")

# 저장/로드 왕복
saved = config.save_config({"max_steps": 33, "release_on_exit": "on",
                            "ollama_servers": [{"name": "거실", "url": "10.0.0.9:11434"}]})
loaded = config.load_config()
check("저장/로드 max_steps", loaded["max_steps"] == 33)
check("저장/로드 release", loaded["release_on_exit"] == "on")
check("저장/로드 서버", loaded["ollama_servers"][0]["name"] == "거실")

# --------------------------------------------------------------------------- #
# 7) 진단 로그
# --------------------------------------------------------------------------- #
print("\n[7] 진단 로그")
diag.clear()
try:
    raise ValueError("테스트 오류")
except Exception as e:
    diag.log_error("test", e)
txt = diag.tail(5)
check("오류 기록", "ValueError" in txt and "테스트 오류" in txt)
check("로그 경로", str(diag.LOG_PATH).endswith("error.log"))

# --------------------------------------------------------------------------- #
# 8) 버전 / UI 문법
# --------------------------------------------------------------------------- #
print("\n[8] 버전 / UI")
check("버전 1.5.0", config.VERSION == "1.5.0")

node = shutil.which("node")
if node:
    import re
    html = (ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    tmp_js = Path(tempfile.mkdtemp(prefix="cb_js_")) / "ui.js"
    tmp_js.write_text(scripts[-1], encoding="utf-8")
    r = subprocess.run([node, "--check", str(tmp_js)], capture_output=True, text=True)
    check("UI 스크립트 문법", r.returncode == 0, r.stderr[:400])
    for el in ("tab-perf", "srvList", "cleanupBtn", "memBadge", "pfTier",
               "release_on_exit", "ollama_servers"):
        check(f"UI 요소 {el}", el in html)
else:
    print("  ℹ node 없음 — UI 문법 검사 생략")

# --------------------------------------------------------------------------- #
print(f"\n결과: {PASS} 통과 · {FAIL} 실패")
sys.exit(1 if FAIL else 0)
