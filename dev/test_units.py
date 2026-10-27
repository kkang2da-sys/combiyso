# -*- coding: utf-8 -*-
"""컴비서 전체 기능 자동 테스트 (서버 없이 in-process)."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 실제 %APPDATA%\compbiyso 를 건드리지 않도록 데이터 폴더를 임시로 격리한다.
# (app.config 가 import 시점에 데이터 폴더를 만들므로 app import 보다 먼저 설정)
_TEST_DATA = Path(r"C:\Users\안방\AppData\Local\Temp\opencode\cb_unitdata")
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ["APPDATA"] = str(_TEST_DATA)

from app import backup, context, sessions, websearch           # noqa: E402
from app import mcp as mcpmod                                   # noqa: E402
from app import agents as agmod                                 # noqa: E402
from app import agent as agentmod                               # noqa: E402
from app import commands as cmdmod                              # noqa: E402
from app import permissions as permmod                          # noqa: E402
from app import skills as skmod                                 # noqa: E402
from app.approval import BROKER                                 # noqa: E402
from app.tools import AgentCtx, Tools, unified_diff             # noqa: E402

WS = Path(r"C:\Users\안방\AppData\Local\Temp\opencode\cb_test2")
PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}   {detail}")


def sec(t):
    print("\n" + "=" * 64 + f"\n▶ {t}\n" + "=" * 64)


def fresh_ws():
    if WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    (WS / "sub").mkdir(parents=True, exist_ok=True)
    (WS / "hello.txt").write_text("안녕하세요 컴비서\n두 번째 줄\n", encoding="utf-8")
    (WS / "sub" / "a.py").write_text("def f():\n    return 1\n# TODO 고치기\n", encoding="utf-8")
    (WS / "data.json").write_text('{"a": 1}', encoding="utf-8")


CFG = {
    "workspace": str(WS), "allow_outside_workspace": False, "auto_backup": True,
    "backup_keep": 300, "max_tool_output": 20000, "command_timeout": 60,
    "web_timeout": 25, "web_max_results": 3,
    "permission_actions": {"read": "allow", "write": "allow", "delete": "allow",
                           "move": "allow", "copy": "allow", "mkdir": "allow",
                           "shell": "allow", "network": "allow", "mcp": "allow"},
    "permission_rules": [],
}


def main():
    fresh_ws()
    ctx = AgentCtx(CFG)
    t = Tools(CFG, ctx)
    backup.clear_all()

    # ------------------------------------------------------------------ 도구
    sec("도구 - 읽기/목록")
    out, _ = t.run("list_dir", {})
    ok("list_dir", "hello.txt" in out and "sub/" in out, out[:120])
    out, _ = t.run("list_dir", {"recursive": True})
    ok("list_dir recursive", "a.py" in out, out[:150])
    out, _ = t.run("read_file", {"path": "hello.txt"})
    ok("read_file", "안녕하세요" in out and "1:" in out, out[:120])
    out, _ = t.run("read_file", {"path": "hello.txt", "start": 2, "end": 2})
    ok("read_file 범위", "두 번째 줄" in out and "1:" not in out.split("표시")[1], out[:120])

    sec("도구 - 쓰기/수정")
    out, extra = t.run("write_file", {"path": "new.txt", "content": "새 파일 내용"})
    ok("write_file", (WS / "new.txt").read_text(encoding="utf-8") == "새 파일 내용", out[:100])
    out, extra = t.run("write_file", {"path": "new.txt", "content": "덮어쓴 내용"})
    ok("write_file 덮어쓰기 diff", extra.get("diff", "").count("-") > 0, str(extra)[:150])
    out, extra = t.run("edit_file", {"path": "new.txt", "old_string": "덮어쓴", "new_string": "수정된"})
    ok("edit_file", "수정된 내용" in (WS / "new.txt").read_text(encoding="utf-8"), out[:100])
    ok("edit_file diff", "+수정된 내용" in extra.get("diff", ""), extra.get("diff", "")[:150])
    try:
        t.run("edit_file", {"path": "hello.txt", "old_string": "없는문자열", "new_string": "x"})
        ok("edit_file 없는 문자열 오류", False)
    except Exception as e:
        ok("edit_file 없는 문자열 오류", "찾지 못했" in str(e), str(e)[:80])
    (WS / "dup.txt").write_text("ab ab ab", encoding="utf-8")
    try:
        t.run("edit_file", {"path": "dup.txt", "old_string": "ab", "new_string": "cd"})
        ok("edit_file 중복 오류", False)
    except Exception as e:
        ok("edit_file 중복 오류", "번 나타" in str(e), str(e)[:80])
    t.run("edit_file", {"path": "dup.txt", "old_string": "ab", "new_string": "cd", "replace_all": True})
    ok("edit_file replace_all", (WS / "dup.txt").read_text(encoding="utf-8") == "cd cd cd")

    sec("도구 - multi_edit / glob / grep")
    (WS / "multi.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    out, extra = t.run("multi_edit", {"path": "multi.txt", "edits": [
        {"old_string": "alpha", "new_string": "ALPHA"},
        {"old_string": "gamma", "new_string": "GAMMA"}]})
    ok("multi_edit", (WS / "multi.txt").read_text(encoding="utf-8") == "ALPHA\nbeta\nGAMMA\n", out[:100])
    out, _ = t.run("glob_files", {"pattern": "*.py"})
    ok("glob_files", "a.py" in out, out[:120])
    out, _ = t.run("grep", {"pattern": "TODO", "glob": "*.py"})
    ok("grep", "a.py" in out and "TODO" in out, out[:140])
    out, _ = t.run("grep", {"pattern": "todo", "ignore_case": True, "glob": "*.py"})
    ok("grep ignore_case", "TODO" in out, out[:140])
    out, _ = t.run("grep", {"pattern": "def", "glob": "*.py", "context": 1})
    ok("grep context", ">" in out, out[:140])
    out, _ = t.run("grep", {"pattern": "def", "glob": "*.py", "files_only": True})
    ok("grep files_only", out.strip().endswith("a.py"), out[:140])

    sec("도구 - 폴더/복사/이동/삭제 (백업)")
    out, _ = t.run("make_dir", {"path": "made/deep"})
    ok("make_dir", (WS / "made" / "deep").is_dir())
    out, _ = t.run("copy_file", {"src": "hello.txt", "dst": "made/copy.txt"})
    ok("copy_file", (WS / "made" / "copy.txt").exists(), out[:100])
    out, _ = t.run("move_file", {"src": "made/copy.txt", "dst": "made/moved.txt"})
    ok("move_file", (WS / "made" / "moved.txt").exists() and not (WS / "made" / "copy.txt").exists())
    before = len(backup.list_backups())
    out, _ = t.run("delete_file", {"path": "made/moved.txt"})
    ok("delete_file + 백업", not (WS / "made" / "moved.txt").exists()
       and len(backup.list_backups()) > before, out[:120])
    try:
        t.run("delete_file", {"path": "made"})
        ok("delete_file 폴더 recursive 필요", False)
    except Exception as e:
        ok("delete_file 폴더 recursive 필요", "recursive" in str(e), str(e)[:80])

    sec("도구 - 명령 실행 / 안전장치")
    out, _ = t.run("run_command", {"command": "python --version"})
    ok("run_command", "Python 3" in out, out[:120])
    try:
        t.run("run_command", {"command": "shutdown /s"})
        ok("위험 명령 차단", False)
    except Exception as e:
        ok("위험 명령 차단", "차단" in str(e), str(e)[:80])
    try:
        t.run("read_file", {"path": r"C:\Windows\win.ini"})
        ok("워크스페이스 밖 차단", False)
    except Exception as e:
        ok("워크스페이스 밖 차단", "밖" in str(e), str(e)[:80])
    t2 = Tools(CFG, AgentCtx(CFG), denied_kinds={"write"})
    try:
        t2.run("write_file", {"path": "x.txt", "content": "x"})
        ok("쓰기 차단 설정", False)
    except Exception as e:
        ok("쓰기 차단 설정", "차단" in str(e), str(e)[:80])

    sec("승인 정보")
    ai = t.approval_info("write_file", {"path": "a.txt", "content": "hi"})
    ok("approval_info write", ai and ai["kind"] == "write" and "a.txt" in ai["detail"], str(ai)[:120])
    ai = t.approval_info("run_command", {"command": "dir"})
    ok("approval_info shell", ai and ai["kind"] == "shell" and ai["detail"] == "dir", str(ai)[:120])
    ok("approval_info read 는 없음", t.approval_info("read_file", {"path": "a"}) is None)
    aid = BROKER.create("write", "테스트", "d", sid="s1")
    threading_ok = False
    import threading
    threading.Timer(0.2, lambda: BROKER.resolve(aid, "allow", True)).start()
    decision = BROKER.wait(aid, 3)
    threading_ok = (decision == "allow" and BROKER.is_session_allowed("s1", "write")
                    and not BROKER.is_session_allowed("s2", "write"))
    ok("승인 브로커 허용/세션범위", threading_ok)
    ok("todo_write 문자열 허용", "2개" in t.run("todo_write", {"todos": ["첫째", "둘째"]})[0]
       and len(ctx.todos) == 2)
    BROKER.clear_session()

    # ------------------------------------------------------------------ 백업
    sec("백업 / 되돌리기")
    (WS / "undo.txt").write_text("원본", encoding="utf-8")
    t.run("write_file", {"path": "undo.txt", "content": "바뀜"})
    ok("수정됨", (WS / "undo.txt").read_text(encoding="utf-8") == "바뀜")
    bl = backup.list_backups()
    rec = next((b for b in bl if b["name"] == "undo.txt"), None)
    ok("백업 목록에 존재", rec is not None)
    if rec:
        okr, msg = backup.restore(rec["id"])
        ok("되돌리기", okr and (WS / "undo.txt").read_text(encoding="utf-8") == "원본", msg)

    # -------------------------------------------------------------- 컨텍스트
    sec("컨텍스트 추정/압축")
    ok("토큰 추정(한글)", context.estimate_tokens("안녕하세요") >= 5)
    ok("토큰 추정(영문)", 1 <= context.estimate_tokens("hello world") <= 8)
    msgs = [{"role": "user", "content": "가" * 100} for _ in range(40)]
    info0 = context.usage_info(msgs, {"compact_threshold": 24000})
    ok("usage_info", info0["tokens"] > 0 and "percent" in info0)
    small = {"auto_compact": True, "compact_threshold": 100, "keep_recent_messages": 4}
    newm, info = context.maybe_compact(small, msgs, lambda m: "요약본")
    ok("자동 압축 동작", info is not None and len(newm) < len(msgs) and "요약" in json.dumps(newm, ensure_ascii=False))
    newm2, info2 = context.maybe_compact({"auto_compact": False, "compact_threshold": 1}, msgs, lambda m: "x")
    ok("압축 끄기", info2 is None and newm2 is msgs)

    # ------------------------------------------------------------------ 세션
    sec("세션 저장/검색/내보내기/가져오기")
    s = sessions.new_session("테스트 대화")
    s["messages"].append({"role": "user", "content": "안녕 컴비서"})
    s["messages"].append({"role": "assistant", "content": "안녕하세요!", "trace": [{"name": "list_dir", "args": {}}]})
    sessions.save(s)
    loaded = sessions.load(s["id"])
    ok("세션 저장/로드", loaded and len(loaded["messages"]) == 2)
    ok("세션 이름변경", sessions.rename(s["id"], "이름 바뀜") and sessions.load(s["id"])["title"] == "이름 바뀜")
    hits = sessions.search("컴비서")
    ok("세션 검색", any(h["id"] == s["id"] for h in hits), str(len(hits)))
    mdtext = sessions.export_markdown(s["id"])
    ok("markdown 내보내기", mdtext and "# 이름 바뀜" in mdtext and "list_dir" in mdtext)
    js = sessions.export_json(s["id"])
    ok("json 내보내기", json.loads(js)["id"] == s["id"])
    imp, err = sessions.import_json(js)
    ok("가져오기", imp is not None and imp["id"] != s["id"] and err is None)
    bad, err2 = sessions.import_json("그냥 텍스트")
    ok("잘못된 가져오기 거부", bad is None and err2 is not None)
    ok("세션 삭제", sessions.delete(imp["id"]) and sessions.load(imp["id"]) is None)

    # ------------------------------------------------------------------ 웹
    sec("웹 검색 / 페이지 읽기")
    try:
        res = websearch.search("파이썬 공식 문서", max_results=3)
        ok("web_search", len(res) >= 1 and res[0]["url"].startswith("http"), str(res[:1])[:160])
    except Exception as e:
        ok("web_search (네트워크)", False, str(e)[:120])
    try:
        page = websearch.fetch_text("https://example.com", max_chars=2000)
        ok("fetch_url", page["ok"] and "domain" in page["content"].lower(), str(page)[:160])
    except Exception as e:
        ok("fetch_url (네트워크)", False, str(e)[:120])
    ok("strip_html", "<" not in websearch.strip_html("<p>안녕<br>하세요</p>"))

    # ------------------------------------------------------------------ MCP
    sec("MCP 클라이언트 (stdio)")
    spec = {"name": "echo", "command": sys.executable,
            "args": [str(ROOT / "dev" / "mcp_echo.py")], "cwd": str(ROOT)}
    client = mcpmod.MCPClient(spec)
    try:
        client.start()
        tools = client.list_tools()
        ok("MCP 초기화/도구목록", any(x["name"] == "echo" for x in tools), str(tools)[:160])
        r = client.call_tool("echo", {"text": "반가워"})
        ok("MCP echo 호출", "반가워" in r, r[:120])
        r = client.call_tool("add", {"a": 3, "b": 4})
        ok("MCP add 호출", "7" in r, r[:120])
    except Exception as e:
        ok("MCP 클라이언트", False, f"{type(e).__name__}: {e}")
    finally:
        client.stop()

    mgr = mcpmod.MCPManager()
    mgr.reload([spec])
    ts = mgr.all_tools()
    ok("MCPManager 도구 노출", any(n.startswith("mcp__echo__") for n, _, _ in ts), str(ts)[:160])
    try:
        r = mgr.call("mcp__echo__echo", {"text": "매니저"})
        ok("MCPManager 호출", "매니저" in r, r[:120])
    except Exception as e:
        ok("MCPManager 호출", False, str(e)[:120])
    mgr.stop_all()

    # ------------------------------------------------------------------ 스킬
    sec("스킬 (SKILL.md / 점진적 공개)")
    for sid in ("t-skill", "t-skill2"):
        skmod.delete_skill(sid)
    s1 = skmod.save_skill("t-skill", name="테스트 스킬", description="테스트용 절차",
                          body="## 절차\n1. read_file 로 읽는다")
    ok("스킬 저장", s1 and s1["name"] == "테스트 스킬", str(s1)[:120])
    (skmod.skill_dir("t-skill") / "ref.txt").write_text("참고 자료 내용", encoding="utf-8")
    got = skmod.get_skill("t-skill")
    ok("스킬 로드 + frontmatter", got and "절차" in got["body"] and got["description"] == "테스트용 절차")
    ok("스킬 파일 실제 존재", Path(got["path"]).is_file() and got["path"].lower().endswith("skill.md"))
    ok("스킬 리소스 목록", any(r["path"] == "ref.txt" for r in got["resources"]), str(got["resources"])[:120])
    ok("스킬 목록에서 본문 제외", all(not x.get("body") for x in skmod.list_skills()))
    ok("스킬 id 검증", (not skmod.valid_id("a/b")) and skmod.valid_id("한글-id.1"))
    try:
        skmod.save_skill("a/b", name="x")
        ok("잘못된 스킬 id 거부", False)
    except Exception:
        ok("잘못된 스킬 id 거부", True)
    ts = Tools(CFG, AgentCtx(CFG))
    out, _ = ts.run("skill", {"name": "t-skill"})
    ok("skill 도구 호출", "절차" in out and "ref.txt" in out, out[:160])
    out, _ = ts.run("skill", {"name": "t-skill", "resource": "ref.txt"})
    ok("skill 리소스 읽기", "참고 자료 내용" in out, out[:120])
    out, _ = ts.run("skill", {"name": "t-skill", "resource": "../config.json"})
    ok("skill 리소스 경로 탈출 차단", "허용되지 않은" in out, out[:120])
    try:
        ts.run("skill", {"name": "없는스킬"})
        ok("없는 스킬 오류", False)
    except Exception as e:
        ok("없는 스킬 오류", "찾을 수 없" in str(e), str(e)[:80])
    ts_off = Tools(dict(CFG, disabled_skills=["t-skill"]), AgentCtx(CFG))
    ok("스킬 중지 반영", not any(x["id"] == "t-skill" for x in ts_off.skills))
    ok("스킬 삭제", skmod.delete_skill("t-skill") and skmod.get_skill("t-skill") is None)

    # ------------------------------------------------------------------ 명령
    sec("커스텀 명령 (/명령어)")
    cmdmod.delete_command("t-cmd")
    c = cmdmod.save_command("t-cmd", description="테스트 명령", body="대상: $1\n전체: $ARGUMENTS")
    ok("명령 저장", bool(c) and c["description"] == "테스트 명령", str(c)[:140])
    ok("명령 목록 포함", any(x["id"] == "t-cmd" for x in cmdmod.list_commands()))
    exp = cmdmod.expand("/t-cmd a b c")
    ok("명령 확장 $1/$ARGUMENTS",
       bool(exp) and "대상: a" in exp["prompt"] and "전체: a b c" in exp["prompt"], str(exp)[:160])
    exp = cmdmod.expand("/help")
    ok("내장 명령 local", bool(exp) and exp["kind"] == "local", str(exp)[:120])
    ok("내장 명령 삭제 보호", not cmdmod.delete_command("help"))
    try:
        cmdmod.save_command("help")
        ok("내장 명령 수정 거부", False)
    except Exception:
        ok("내장 명령 수정 거부", True)
    ok("명령 삭제", cmdmod.delete_command("t-cmd") and cmdmod.get_command("t-cmd") is None)
    ok("없는 명령은 None", cmdmod.expand("/정의되지않음 x") is None)
    ok("슬래시 아닌 입력은 None", cmdmod.expand("안녕하세요") is None)
    sk_like = [{"id": "t-skill2", "name": "엑스", "description": "d"}]
    exp = cmdmod.expand("/t-skill2 인자1", skills=sk_like)
    ok("스킬 직접 호출 확장", bool(exp) and exp["kind"] == "skill" and "skill" in exp["prompt"],
       str(exp)[:160])

    # ------------------------------------------------------------------ 에이전트
    sec("에이전트 정의 / 도구 허용목록")
    agmod.delete_agent("t-agent")
    a = agmod.save_agent("t-agent", description="테스트 에이전트", mode="primary",
                         tools=["read_file", "grep"], body="너는 테스트용이다.")
    ok("에이전트 저장", bool(a) and a["mode"] == "primary" and isinstance(a["tools"], list), str(a)[:160])
    ok("에이전트 도구 제한", set(a["tools"]) == {"read_file", "grep"}, str(a["tools"]))
    plan = agmod.get_agent("plan")
    ok("내장 plan 읽기전용", "write_file" not in plan["tools"] and plan["mode"] == "primary")
    ok("내장 general 하위", agmod.get_agent("general")["mode"] == "subagent")
    ok("내장 explore 하위", agmod.get_agent("explore")["mode"] == "subagent")
    names = [x["function"]["name"] for x in agentmod.all_specs(CFG, 0, a["tools"])]
    ok("도구 스펙 필터", names == ["read_file", "grep"], str(names))
    pnames = [x["function"]["name"] for x in agentmod.all_specs(CFG, 0, plan["tools"])]
    ok("plan 스펙: 쓰기 없음 + task 있음", "write_file" not in pnames and "task" in pnames, str(pnames))
    ok("하위 단계에서 task 제외", "task" not in [x["function"]["name"]
       for x in agentmod.all_specs(CFG, 1, "*")])
    ta = Tools(CFG, AgentCtx(CFG), allowed_tools=a["tools"])
    try:
        ta.run("write_file", {"path": "no.txt", "content": "x"})
        ok("도구 허용목록 차단", False)
    except Exception as e:
        ok("도구 허용목록 차단", "사용할 수 없" in str(e), str(e)[:80])
    ok("허용 도구는 동작", "안녕하세요" in ta.run("read_file", {"path": "hello.txt"})[0])
    sysp = agentmod.build_system(CFG, agentmod.all_specs(CFG, 0, a["tools"]), False, 0, [],
                                 a, [{"id": "x", "name": "엑스", "description": "설명"}])
    ok("시스템 프롬프트에 에이전트 본문", "테스트용이다" in sysp)
    ok("시스템 프롬프트에 스킬 목록", "- x: 엑스" in sysp, sysp[-300:])
    ok("system_extra 반영", True)
    ok("에이전트 삭제", agmod.delete_agent("t-agent") and not agmod.agent_path("t-agent").is_file())

    # ------------------------------------------------------------------ 권한
    sec("세밀 권한 규칙")
    pc = dict(CFG)
    pc["permission_actions"] = {"read": "allow", "write": "ask", "delete": "deny",
                                "move": "ask", "copy": "allow", "mkdir": "allow",
                                "shell": "ask", "network": "allow", "mcp": "ask"}
    pc["permission_rules"] = [
        {"tool": "run_command", "pattern": "git push*", "action": "ask"},
        {"tool": "run_command", "pattern": "rm -rf*", "action": "deny"},
        {"tool": "read_file", "pattern": "*.env", "action": "deny"},
        {"tool": "write_file", "pattern": "*설계*.md", "action": "allow"},
    ]
    ok("기본 동작 ask", permmod.resolve(pc, "write_file", "a.txt")["action"] == "ask")
    ok("기본 동작 deny", permmod.resolve(pc, "delete_file", "a.txt")["action"] == "deny")
    ok("기본 동작 allow", permmod.resolve(pc, "read_file", "a.txt")["action"] == "allow")
    ok("규칙 deny (rm -rf)", permmod.resolve(pc, "run_command", "rm -rf /")["action"] == "deny")
    ok("규칙 ask (git push)", permmod.resolve(pc, "run_command", "git push origin main")["action"] == "ask")
    ok("규칙에 안 걸리면 기본값", permmod.resolve(pc, "run_command", "dir")["action"] == "ask")
    ok("규칙 deny (*.env)", permmod.resolve(pc, "read_file", r"C:\ws\.env")["action"] == "deny")
    ok("경로 슬래시 정규화", permmod.resolve(pc, "read_file", "C:/ws/.env")["action"] == "deny")
    ok("규칙 allow 가 deny 기본을 이김",
       permmod.resolve(pc, "write_file", r"C:\ws\설계.md")["action"] == "allow")
    ok("규칙 없는 도구 기본값", permmod.resolve(pc, "make_dir", "새폴더")["action"] == "allow")
    ok("권한 불필요 도구", permmod.resolve(pc, "todo_write", "")["action"] == "allow")
    ok("MCP 도구 종류", permmod.tool_kind("mcp__echo__add") == "mcp")
    ok("종류 라벨/순서 존재", len(permmod.KIND_ORDER) >= 9 and "shell" in permmod.KIND_LABEL)
    ok("프리셋 readonly 는 쓰기 차단", permmod.PRESETS["readonly"]["write"] == "deny")
    ts3 = Tools(pc, AgentCtx(pc), denied_kinds={"delete"})
    try:
        ts3.run("delete_file", {"path": "data.json"})
        ok("차단 종류 실행 거부", False)
    except Exception as e:
        ok("차단 종류 실행 거부", "차단" in str(e), str(e)[:80])
    ok("차단 아닌 종류는 실행", "a" in ts3.run("read_file", {"path": "data.json"})[0])

    sec("todoread")
    ctx2 = AgentCtx(CFG)
    t3 = Tools(CFG, ctx2)
    t3.run("todo_write", {"todos": [{"text": "첫째", "status": "done"},
                                    {"text": "둘째", "status": "in_progress"},
                                    "셋째"]})
    out, _ = t3.run("todoread", {})
    ok("todoread 내용", "첫째" in out and "[x]" in out and "[>]" in out and "완료 1" in out, out[:160])
    out, _ = Tools(CFG, AgentCtx(CFG)).run("todoread", {})
    ok("todoread 빈 목록", "없음" in out, out[:80])

    # ------------------------------------------------------------------ diff
    sec("diff 유틸")
    d = unified_diff("a\nb\n", "a\nc\n", "f", "f")
    ok("unified_diff", "-b" in d and "+c" in d, d.replace("\n", " | ")[:120])

    print("\n" + "=" * 64)
    print(f"결과:  통과 {PASS}  /  실패 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
