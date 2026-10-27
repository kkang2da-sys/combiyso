# -*- coding: utf-8 -*-
"""컴비서 HTTP 통합 테스트 (서버가 떠 있는 상태에서 실행).

주의: 8799 포트에 사용자가 실제로 쓰는 앱이 떠 있으면 이 테스트가 그 앱의
설정을 덮어쓰고 세션을 삭제한다. 그래서 아래 assert_isolated() 가
서버의 데이터 폴더(_data_dir)가 임시 폴더가 아니면 즉시 중단한다.
반드시 `$env:APPDATA="$env:TEMP\\cb_test"` 로 띄운 서버에만 실행할 것.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8799"
WS = r"C:\Users\안방\AppData\Local\Temp\opencode\cb_test3"
PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}   {str(detail)[:200]}")


def sec(t):
    print("\n" + "=" * 64 + f"\n▶ {t}\n" + "=" * 64)


def wait_health(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if requests.get(BASE + "/api/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def chat(message, sid=None, approve="remember", show=True):
    """승인 요청이 오면 자동 응답하며 SSE 를 소비한다."""
    body = {"session_id": sid, "message": message}
    events, text, tools, approvals = [], "", [], 0
    cur = sid
    if show:
        print(f"\n[요청] {message}")
    with requests.post(BASE + "/api/chat", json=body, stream=True, timeout=900) as r:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            events.append(ev)
            t = ev.get("type")
            if t == "session":
                cur = ev["id"]
            elif t == "delta":
                text += ev["text"]
            elif t == "reset":
                text = ""
            elif t == "final":
                text = ev["text"]
            elif t == "tool":
                tools.append((ev["name"], ev.get("args")))
                if show:
                    print(f"  🔧 {ev['name']} {json.dumps(ev.get('args'), ensure_ascii=False)[:110]}")
            elif t == "tool_result":
                if show:
                    print(f"     ↳ {'OK' if ev['ok'] else 'ERR'} {str(ev['result']).replace(chr(10),' | ')[:130]}")
            elif t == "approval":
                approvals += 1
                if show:
                    print(f"  🔐 승인요청: {ev['title']} — {str(ev['detail'])[:90]}")
                dec = {"allow": "allow", "remember": "allow", "deny": "deny"}[approve]
                requests.post(BASE + "/api/approve",
                              json={"id": ev["id"], "decision": dec,
                                    "remember": approve == "remember"}, timeout=10)
            elif t == "subagent":
                if show:
                    print(f"  🤖 하위에이전트 {ev['status']} {str(ev.get('prompt') or ev.get('text') or '')[:80]}")
            elif t == "todos":
                if show:
                    print("  📋 할일:", [x["text"] for x in ev["todos"]])
            elif t == "error":
                if show:
                    print("  ⚠", ev["message"])
    if show:
        print(f"[답변] {text[:400]}")
    return {"sid": cur, "text": text, "tools": tools, "events": events, "approvals": approvals}


def names(res):
    return [n for n, _ in res["tools"]]


def assert_isolated():
    """서버가 테스트용 임시 데이터 폴더를 쓰는지 확인한다.

    실제 사용자 앱(%APPDATA%\\compbiyso)에 붙으면 세션 전체 삭제·설정 덮어쓰기로
    사용자 데이터가 소실되므로, 그런 경우에는 테스트를 시작하지 않는다.
    """
    try:
        c = requests.get(BASE + "/api/config", timeout=5).json()
    except Exception as e:                                   # noqa: BLE001
        print(f"서버 설정 확인 실패: {e}")
        return False
    data_dir = str(c.get("_data_dir") or "")
    tmp_roots = [p for p in (os.environ.get("TEMP"), os.environ.get("TMP"), "/tmp") if p]
    isolated = bool(data_dir) and any(
        os.path.abspath(data_dir).lower().startswith(os.path.abspath(t).lower())
        for t in tmp_roots)
    if isolated or os.environ.get("COMPIYSO_TEST_ALLOW_REAL") == "1":
        print(f"테스트 서버 data_dir = {data_dir}")
        return True
    print("\n" + "!" * 64)
    print("[중단] 8799 포트의 서버가 테스트용 데이터 폴더를 쓰지 않습니다.")
    print(f"        data_dir = {data_dir}")
    print("        실제 사용자 데이터일 수 있어 테스트를 시작하지 않습니다.")
    print("        테스트용 서버를 먼저 띄우세요:")
    print('          $env:APPDATA="$env:TEMP\\cb_test"; python run.py --no-browser')
    print("        (정말 강제로 실행하려면 COMPIYSO_TEST_ALLOW_REAL=1)")
    print("!" * 64)
    return False


def main():
    if not wait_health():
        print("서버 응답 없음")
        return 1
    print("서버 연결 OK")
    if not assert_isolated():
        return 2

    Path(WS).mkdir(parents=True, exist_ok=True)
    for f in Path(WS).iterdir():
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
        except Exception:
            pass
    (Path(WS) / "hello.txt").write_text("안녕하세요 컴비서\n", encoding="utf-8")
    (Path(WS) / "code.py").write_text("def add(a,b):\n    return a+b\n# TODO: 곱셈도\n", encoding="utf-8")

    # ------------------------------------------------------------ 기본 API
    sec("기본 API")
    h = requests.get(BASE + "/api/health").json()
    ok("health", h.get("status") == "ok" and h.get("version"), h)
    ui = requests.get(BASE + "/").text
    ok("UI 페이지", "컴비서" in ui and "apOverlay" in ui and "mcpEditor" in ui and len(ui) > 20000, len(ui))
    ok("UI: 신규 기능 요소", all(k in ui for k in
       ("slashMenu", "permKinds", "permRules", "skillsList", "agentsList", "cmdsList",
        "skillOverlay", "agentEditOverlay", "cmdOverlay", "agentPick")))
    ok("favicon", requests.get(BASE + "/favicon.ico").status_code == 200)
    meta = requests.get(BASE + "/api/meta").json()
    ok("메타(권한 종류/프리셋)", len(meta.get("kind_order", [])) >= 9
       and "readonly" in meta.get("presets", {}), str(meta)[:160])
    tl = requests.get(BASE + "/api/tools").json()
    ok("도구 목록", any(t["name"] == "multi_edit" for t in tl["tools"])
       and any(t["name"] == "web_search" for t in tl["tools"]), len(tl["tools"]))
    ok("신규 도구 포함", any(t["name"] == "todoread" for t in tl["tools"])
       and any(t["name"] == "skill" for t in tl["tools"]), [t["name"] for t in tl["tools"]])
    ok("도구 허용 표시", all("allowed" in t for t in tl["tools"]))
    fs = requests.get(BASE + "/api/fs").json()
    ok("fs 루트", fs.get("root") and fs.get("dirs"), str(fs)[:120])

    # ------------------------------------------------------------ 설정
    sec("설정")
    cfg = requests.get(BASE + "/api/config").json()
    cfg.update({
        "provider": "ollama", "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "gemma4:12b", "workspace": WS,
        "permission_actions": {"read": "allow", "write": "ask", "delete": "ask",
                               "move": "ask", "copy": "allow", "mkdir": "allow",
                               "shell": "ask", "network": "allow", "mcp": "allow"},
        "permission_rules": [], "agent": "build", "skills_enabled": True,
        "commands_enabled": True, "auto_backup": True,
        "max_steps": 12, "temperature": 0.2, "auto_compact": True,
        "compact_threshold": 24000, "use_rules_file": True, "rules_file": "COMPIYSO.md",
        "mcp_servers": [{"name": "echo", "command": sys.executable,
                         "args": [str(ROOT / "dev" / "mcp_echo.py")],
                         "cwd": str(ROOT), "enabled": True}],
    })
    r = requests.post(BASE + "/api/config", json=cfg)
    ok("설정 저장", r.status_code == 200 and r.json()["workspace"] == WS)
    time.sleep(6)   # MCP 백그라운드 연결 대기
    m = requests.get(BASE + "/api/mcp").json()
    ok("MCP 상태", any(s["name"] == "echo" and s["alive"] for s in m["servers"]), str(m)[:200])
    tl = requests.get(BASE + "/api/tools").json()
    ok("MCP 도구가 목록에", any(t["name"].startswith("mcp__echo__") for t in tl["tools"]),
       [t["name"] for t in tl["tools"]][-3:])
    ok("모델 목록", requests.get(BASE + "/api/models").json().get("ok"))
    o = requests.get(BASE + "/api/ollama").json()
    ok("Ollama 정보", o.get("ok") and len(o.get("models", [])) >= 1, str(o)[:150])
    t = requests.post(BASE + "/api/test", json=cfg).json()
    ok("연결 테스트", t.get("ok"), t)

    # ------------------------------------------------------------ 스킬 API
    sec("스킬 API")
    requests.delete(BASE + "/api/skills/t-skill")
    sr = requests.post(BASE + "/api/skills", json={
        "id": "t-skill", "name": "테스트 스킬",
        "description": "테스트용 절차", "body": "## 절차\n1. read_file"}).json()
    ok("스킬 생성", sr.get("ok") and sr["skill"]["name"] == "테스트 스킬", str(sr)[:150])
    sl = requests.get(BASE + "/api/skills").json()
    ok("스킬 목록", any(s["id"] == "t-skill" and s["enabled"] for s in sl["skills"]), str(sl)[:150])
    sd = requests.get(BASE + "/api/skills/t-skill").json()
    ok("스킬 상세(본문 포함)", "절차" in sd.get("body", ""), str(sd)[:120])
    tg = requests.post(BASE + "/api/skills/t-skill/toggle", json={"enabled": False}).json()
    ok("스킬 중지 토글", tg.get("ok") and "t-skill" in tg.get("disabled", []), str(tg)[:120])
    requests.post(BASE + "/api/skills/t-skill/toggle", json={"enabled": True})
    bad = requests.post(BASE + "/api/skills", json={"id": "a/b", "name": "x"}).json()
    ok("잘못된 스킬 id 거부", not bad.get("ok"), str(bad)[:120])

    # ------------------------------------------------------------ 명령 API
    sec("명령 API")
    requests.delete(BASE + "/api/commands/t-cmd")
    cr = requests.post(BASE + "/api/commands", json={
        "id": "t-cmd", "description": "테스트 명령", "body": "대상 파일: $1"}).json()
    ok("명령 생성", cr.get("ok") and cr["command"]["id"] == "t-cmd", str(cr)[:150])
    cl = requests.get(BASE + "/api/commands").json()["commands"]
    ok("명령 목록(내장+사용자)", any(c["id"] == "help" and c["builtin"] for c in cl)
       and any(c["id"] == "t-cmd" for c in cl), [c["id"] for c in cl])
    ok("내장 명령 수정 보호", not requests.post(BASE + "/api/commands",
       json={"id": "help", "description": "x", "body": "x"}).json().get("ok"))

    # ------------------------------------------------------------ 에이전트 API
    sec("에이전트 API")
    ar = requests.get(BASE + "/api/agents").json()["agents"]
    ok("내장 에이전트 4종", {a["id"] for a in ar} >= {"build", "plan", "general", "explore"},
       [a["id"] for a in ar])
    requests.delete(BASE + "/api/agents/t-agent")
    asv = requests.post(BASE + "/api/agents", json={
        "id": "t-agent", "description": "테스트", "mode": "primary",
        "tools": ["read_file", "grep"], "body": "테스트 에이전트다."}).json()
    ok("에이전트 생성", asv.get("ok") and asv["agent"]["tools"] == ["read_file", "grep"], str(asv)[:150])
    ag = requests.get(BASE + "/api/agents/t-agent").json()
    ok("에이전트 상세", "테스트 에이전트다" in ag.get("body", ""), str(ag)[:120])
    ok("설정 응답에 에이전트 목록", any(a["id"] == "t-agent"
       for a in requests.get(BASE + "/api/config").json().get("_agents", [])))

    # ------------------------------------------------------------ 슬래시
    sec("슬래시 자동완성 목록")
    si = requests.get(BASE + "/api/slash").json()["items"]
    ok("슬래시: 명령 + 스킬", any(i["id"] == "help" for i in si)
       and any(i["kind"] == "skill" and i["id"] == "t-skill" for i in si), str(si)[:220])

    # ------------------------------------------------------------ 규칙 파일
    sec("규칙 파일")
    rs = requests.get(BASE + "/api/rules").json()
    ok("규칙 조회", "path" in rs, str(rs)[:120])
    wr = requests.post(BASE + "/api/rules",
                       json={"content": "# 규칙\n- 항상 한국어로 답한다\n"}).json()
    ok("규칙 저장", wr.get("ok") and Path(ws := wr["path"]).read_text(encoding="utf-8").startswith("# 규칙"))
    ok("규칙 파일이 워크스페이스에", Path(wr["path"]).parent == Path(WS))

    # ------------------------------------------------------------ 세션 API
    sec("세션 API")
    requests.delete(BASE + "/api/sessions")
    sid = requests.post(BASE + "/api/sessions").json()["id"]
    ok("세션 생성", bool(sid))
    requests.post(BASE + "/api/sessions/" + sid + "/rename", json={"title": "HTTP 테스트"})
    lst = requests.get(BASE + "/api/sessions").json()
    ok("세션 목록", any(s["id"] == sid and s["title"] == "HTTP 테스트" for s in lst), str(lst)[:150])
    ok("세션 검색", any(s["id"] == sid for s in requests.get(BASE + "/api/sessions?q=HTTP").json()))
    ex = requests.get(BASE + "/api/sessions/" + sid + "/export?format=md")
    ok("세션 내보내기", ex.status_code == 200 and "HTTP 테스트" in ex.text,
       ex.headers.get("content-disposition", "")[:80])
    u = requests.get(BASE + "/api/usage?session_id=" + sid).json()
    ok("사용량 API", "tokens" in u and "percent" in u, u)

    # ------------------------------------------------------------ 채팅 1
    sec("채팅 - 목록/읽기 (승인 불필요)")
    r1 = chat("이 작업 폴더에 있는 파일 목록을 보여줘.", sid=sid)
    ok("list_dir 호출", "list_dir" in names(r1), names(r1))
    ok("승인 안 물음", r1["approvals"] == 0, r1["approvals"])
    sid = r1["sid"]
    r2 = chat("hello.txt 내용을 알려줘.", sid=sid)
    ok("read_file 호출", "read_file" in names(r2), names(r2))
    ok("내용 정확", "안녕하세요" in r2["text"], r2["text"][:120])

    # ------------------------------------------------------------ 채팅 2 (승인)
    sec("채팅 - 쓰기 (승인 흐름)")
    r3 = chat("note.txt 라는 파일을 만들어서 '컴비서 완전판 테스트' 라고 저장해줘.", sid=sid)
    ok("승인 요청 발생", r3["approvals"] >= 1, r3["approvals"])
    ok("write_file 호출", "write_file" in names(r3), names(r3))
    p = Path(WS) / "note.txt"
    ok("파일 실제 생성", p.exists() and "컴비서 완전판" in p.read_text(encoding="utf-8"),
       p.read_text(encoding="utf-8") if p.exists() else "없음")
    sid = r3["sid"]
    bl = requests.get(BASE + "/api/backups").json()["backups"]
    ok("백업 API", isinstance(bl, list))

    # ------------------------------------------------------------ 채팅 3 (거부)
    sec("채팅 - 승인 거부 (새 세션)")
    sid_deny = requests.post(BASE + "/api/sessions").json()["id"]
    r4 = chat("거부.txt 파일을 'x' 내용으로 만들어줘.", sid=sid_deny, approve="deny")
    ok("승인 거부 요청 발생", r4["approvals"] >= 1, r4["approvals"])
    ok("거부 시 파일 안 생김", not (Path(WS) / "거부.txt").exists())

    # ------------------------------------------------------------ 채팅 4 (grep)
    sec("채팅 - 검색")
    r5 = chat("code.py 에서 TODO 라고 적힌 부분을 찾아서 알려줘.", sid=sid)
    ok("grep 호출", "grep" in names(r5), names(r5))
    ok("TODO 발견", "TODO" in r5["text"] or "곱셈" in r5["text"], r5["text"][:150])
    sid = r5["sid"]

    # ------------------------------------------------------------ 채팅 5 (셸)
    sec("채팅 - 명령 실행")
    r6 = chat("python --version 을 실행해서 알려줘.", sid=sid)
    ok("run_command 호출", "run_command" in names(r6), names(r6))
    ok("버전 표시", "Python 3" in r6["text"], r6["text"][:120])
    sid = r6["sid"]

    # ------------------------------------------------------------ 채팅 6 (MCP)
    sec("채팅 - MCP 도구")
    r7 = chat("MCP 도구 mcp__echo__add 를 a=10, b=5 로 호출해서 결과를 알려줘.", sid=sid)
    used = [n for n in names(r7) if n.startswith("mcp__")]
    ok("MCP 도구 호출(모델에 따라 다를 수 있음)", len(used) >= 1 or "15" in r7["text"],
       f"도구={names(r7)} 답변={r7['text'][:120]}")
    sid = r7["sid"]

    # ------------------------------------------------------------ 채팅 7 (todo/task)
    sec("채팅 - 할일/하위에이전트")
    r8 = chat("할 일 3개를 todo_write 로 계획해줘: ①폴더 확인 ②파일 읽기 ③보고. 그리고 이 폴더 파일들을 "
              "task 도구로 조사해줘.", sid=sid)
    ok("todo_write 또는 task 사용", ("todo_write" in names(r8)) or ("task" in names(r8)), names(r8))
    sid = r8["sid"]

    # ------------------------------------------------------------ 슬래시 명령
    sec("채팅 - 슬래시 명령 (모델 호출 없이 즉시 응답)")
    r9 = chat("/help", sid=sid)
    ok("/help 즉시 응답", "사용 가능한 명령" in r9["text"] and r9["approvals"] == 0, r9["text"][:150])
    r10 = chat("/tools", sid=r9["sid"])
    ok("/tools 응답", "read_file" in r10["text"], r10["text"][:150])
    r11 = chat("/t-cmd hello.txt", sid=r10["sid"])
    ok("/커스텀명령 확장 실행", bool(r11["text"]) and r11["approvals"] == 0, r11["text"][:150])
    sid = r11["sid"]

    # ------------------------------------------------------------ 스킬 호출 e2e
    sec("채팅 - 스킬 직접 호출 (/스킬id → skill 도구)")
    c_sk = requests.get(BASE + "/api/config").json()
    c_sk["permission_actions"] = dict(c_sk.get("permission_actions") or {},
                                      write="allow", mkdir="allow")
    requests.post(BASE + "/api/config", json=c_sk)
    r12 = chat("/t-skill 작업 폴더 구조를 조사해서 보고서를 만들어줘.", sid=sid)
    ok("스킬 도구 호출(모델에 따라 다를 수 있음)",
       "skill" in names(r12) or "보고서" in r12["text"] or "report" in r12["text"].lower(),
       f"도구={names(r12)} 답변={r12['text'][:110]}")
    sid = r12["sid"]

    # ------------------------------------------------------------ 권한 규칙 차단
    sec("권한 규칙 - deny 는 승인도 묻지 않고 차단")
    c2 = requests.get(BASE + "/api/config").json()
    c2["permission_rules"] = [{"tool": "write_file", "pattern": "*차단*", "action": "deny"}]
    requests.post(BASE + "/api/config", json=c2)
    sid_p = requests.post(BASE + "/api/sessions").json()["id"]
    rp = chat("차단파일.txt 를 'x' 내용으로 만들어줘.", sid=sid_p)
    ok("규칙 차단: 승인 안 물음 + 파일 미생성",
       rp["approvals"] == 0 and not (Path(WS) / "차단파일.txt").exists(),
       f"승인={rp['approvals']} 도구={names(rp)}")
    c2["permission_rules"] = []
    requests.post(BASE + "/api/config", json=c2)

    # ------------------------------------------------------------ 에이전트 전환
    sec("에이전트 전환 (plan = 읽기 전용)")
    c3 = requests.get(BASE + "/api/config").json()
    c3["agent"] = "plan"
    requests.post(BASE + "/api/config", json=c3)
    tl2 = requests.get(BASE + "/api/tools").json()
    wtool = next((x for x in tl2["tools"] if x["name"] == "write_file"), None)
    ok("plan 모드: write_file 비허용", wtool is not None and wtool["allowed"] is False, str(wtool)[:140])
    rtool = next((x for x in tl2["tools"] if x["name"] == "read_file"), None)
    ok("plan 모드: read_file 허용", rtool is not None and rtool["allowed"] is True)
    ok("plan 모드 에이전트 반영", tl2.get("agent") == "plan", tl2.get("agent"))

    # ------------------------------------------------------------ 최종 세션 확인
    sec("세션 최종 상태")
    full = requests.get(BASE + "/api/sessions/" + sid).json()
    roles = [m["role"] for m in full["messages"]]
    ok("대화 저장됨", roles.count("user") >= 7 and roles.count("assistant") >= 7, str(len(roles)))
    ok("도구 기록 저장", any(m.get("trace") for m in full["messages"]))
    ok("제목 자동생성", bool(full.get("title")))
    ex2 = requests.get(BASE + "/api/sessions/" + sid + "/export?format=json").json()
    ok("JSON 내보내기 전체", len(ex2.get("messages", [])) == len(full["messages"]))

    # ------------------------------------------------------------ 중단 복구
    sec("중단 복구 (실행 저널 / 이어서 하기)")
    from app import runs as _runs
    from app import sessions as _sessions

    js = _sessions.new_session()
    js["title"] = "중단 복구 테스트"
    js["messages"].append({"role": "user",
                           "content": "자기소개를 아주 짧게 한 줄로 해줘.",
                           "ts": time.time()})
    _sessions.save(js)
    jrec = _runs.start(js["id"], "build", note="테스트")
    _runs.save_state(jrec["id"],
                     messages=[{"role": "user", "content": js["messages"][0]["content"]}],
                     step=1, phase="생각 중")
    _runs.finish(jrec["id"], "interrupted", "테스트 강제 중단")

    allr = requests.get(BASE + "/api/runs").json()
    mine = [r for r in allr.get("runs", []) if r["id"] == jrec["id"]]
    ok("저널이 API 에 노출됨", len(mine) == 1, allr)
    ok("이어서 가능 표시", bool(mine and mine[0]["resumable"]) and not mine[0]["active"])
    ok("여유 메모리 조회", isinstance(allr.get("free_mb"), int) and allr["free_mb"] > 0,
       allr.get("free_mb"))

    sesslist = requests.get(BASE + "/api/sessions").json()
    item = [x for x in sesslist if x["id"] == js["id"]]
    ok("세션 목록에 재개 표시", bool(item and item[0].get("resumable")))

    events = []
    with requests.post(BASE + "/api/resume", json={"run_id": jrec["id"]},
                       stream=True, timeout=900) as r:
        ok("resume HTTP 200", r.status_code == 200, r.status_code)
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:].strip()))
    types = [e.get("type") for e in events]
    ok("resume: run 이벤트", "run" in types)
    ok("resume: status 이벤트", "status" in types)
    ok("resume: final 이벤트", "final" in types)
    ok("resume: done 이벤트", "done" in types)
    rtext = ""
    for e in events:
        if e.get("type") == "final":
            rtext = e.get("text") or ""
    ok("resume: 답변 생성", len(rtext) > 3, rtext[:80])

    full2 = requests.get(BASE + "/api/sessions/" + js["id"]).json()
    roles2 = [m["role"] for m in full2.get("messages", [])]
    ok("resume: 세션에 답변 저장",
       any(m["role"] == "assistant" and (m["content"] or "") for m in full2["messages"]),
       str(roles2))
    ok("resume: 사용자 메시지 중복 없음", roles2.count("user") == 1, str(roles2))
    ok("resume: 실행 완료 처리", (_runs.get(jrec["id"]) or {}).get("status") == "done")

    d = requests.post(BASE + "/api/runs/discard", json={"run_id": jrec["id"]}).json()
    ok("저널 삭제", d.get("ok"))
    requests.delete(BASE + "/api/sessions/" + js["id"])

    # ------------------------------------------------------------ 정리
    sec("정리 (설정 초기화 / 테스트 데이터 삭제)")
    rr = requests.post(BASE + "/api/config/reset").json()
    ok("설정 초기화", rr.get("permission_rules") == [] and rr.get("agent") == "build"
       and rr.get("mcp_servers") == [], str(rr.get("permission_rules"))[:60])
    ok("세션 전체 삭제", requests.delete(BASE + "/api/sessions").json().get("ok"))
    requests.delete(BASE + "/api/backups")
    requests.delete(BASE + "/api/skills/t-skill")
    requests.delete(BASE + "/api/commands/t-cmd")
    requests.delete(BASE + "/api/agents/t-agent")
    ok("테스트 자원 삭제",
       not any(s["id"] == "t-skill" for s in requests.get(BASE + "/api/skills").json()["skills"])
       and not any(c["id"] == "t-cmd" for c in requests.get(BASE + "/api/commands").json()["commands"]))

    print("\n" + "=" * 64)
    print(f"결과:  통과 {PASS}  /  실패 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
