# -*- coding: utf-8 -*-
"""실행 저널(중단 복구) / 에이전트 체크포인트 테스트.

실제 LLM 없이 가짜 llm 으로 돌린다. (Ollama 불필요)
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 데이터 폴더를 임시로 (app import 전에!)
_TMP = Path(tempfile.mkdtemp(prefix="cb_runs_"))
os.environ["APPDATA"] = str(_TMP / "appdata")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent as agentmod          # noqa: E402
from app import runs, sessions             # noqa: E402

WS = _TMP / "ws"
WS.mkdir(parents=True, exist_ok=True)
(WS / "hello.txt").write_text("안녕 컴비서\n", encoding="utf-8")

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}   {str(detail)[:240]}")


def sec(t):
    print("\n" + "=" * 64 + f"\n▶ {t}\n" + "=" * 64)


CFG = {
    "workspace": str(WS), "allow_outside_workspace": False, "auto_backup": True,
    "backup_keep": 50, "max_tool_output": 20000, "command_timeout": 30,
    "web_timeout": 10, "web_max_results": 3, "max_steps": 5,
    "provider": "ollama", "model": "fake", "tool_mode": "native",
    "agent": "build", "context_limit": 100000, "permission_actions": {},
}


class FakeLLM:
    """미리 정한 응답을 순서대로 돌려주는 가짜 LLM."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []          # 각 호출에 전달된 messages 사본

    def chat_stream(self, cfg, messages, tools=None):
        self.seen.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        r = self.replies.pop(0) if self.replies else {"text": "끝", "tool_calls": []}
        if r.get("delta"):
            yield {"delta": r["delta"]}
        yield {"final": True, "text": r.get("text", ""),
               "tool_calls": r.get("tool_calls") or []}

    def complete(self, cfg, messages):
        return "요약"


def drive(gen):
    events = list(gen)
    return events


def main():
    sec("실행 저널 (runs)")
    rec = runs.start("sess-1", "build", note="hi")
    ok("start → running", rec["status"] == "running" and rec["id"].startswith("run_"))
    ok("활성 목록에 포함", any(r["id"] == rec["id"] for r in runs.list_runs()))

    runs.save_state(rec["id"], step=2, phase="도구 실행", detail="read_file",
                    messages=[{"role": "system", "content": "SYS"},
                              {"role": "user", "content": "안녕"},
                              {"role": "assistant", "content": "네"}],
                    stats={"tools": 1})
    got = runs.get(rec["id"])
    ok("save_state 반영", got["step"] == 2 and got["phase"] == "도구 실행")
    ok("시스템 메시지 제거", all(m["role"] != "system" for m in got["state"]["messages"]))
    ok("메시지 2개 보존", len(got["state"]["messages"]) == 2)

    big = "x" * 200000
    runs.save_state(rec["id"], messages=[{"role": "tool", "content": big}])
    ok("긴 내용 잘림", len(runs.get(rec["id"])["state"]["messages"][0]["content"]) <= 8000)

    runs.finish(rec["id"], "interrupted", "테스트 중단")
    got = runs.get(rec["id"])
    ok("중단 상태 기록", got["status"] == "interrupted" and got["error"] == "테스트 중단")
    ok("중단 실행은 이어서 가능", got["status"] in runs.RESUMABLE)
    ok("active_for_session", (runs.active_for_session("sess-1") or {}).get("id") == rec["id"])

    r2 = runs.start("sess-1", "build")
    runs.save_state(r2["id"], messages=[{"role": "user", "content": "x"}])
    runs.finish(r2["id"], "done")
    ok("완료 실행은 state 비움", runs.get(r2["id"])["state"]["messages"] == [])
    ok("완료 실행은 목록에서 제외", all(r["id"] != r2["id"] for r in runs.list_runs()))

    r3 = runs.start("sess-2", "build")
    n = runs.sweep_startup()
    ok("시작 스윕 → 중단 처리", runs.get(r3["id"])["status"] == "interrupted" and n >= 1)

    ok("삭제", runs.discard(r3["id"]) and runs.get(r3["id"]) is None)

    sec("재개 메시지 정리 (repair_messages)")
    msgs = [
        {"role": "user", "content": "1+1?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}}]},
    ]
    fixed = runs.repair_messages(msgs)
    ok("끊긴 tool_call 에 결과 채움",
       fixed[-1]["role"] == "tool" and fixed[-1]["tool_call_id"] == "c1")

    text_msgs = [{"role": "user", "content": "해줘"},
                 {"role": "assistant", "content": '네 <tool>{"name":"x"}</tool>'}]
    fixed2 = runs.repair_messages(text_msgs)
    ok("텍스트모드 끊김 안내", fixed2[-1]["role"] == "user")

    ok("메모리 조회", runs.mem_free_mb() is None or runs.mem_free_mb() > 0)

    sec("에이전트 체크포인트 + 상태 이벤트")
    ck = []
    fake = FakeLLM([{"text": "안녕하세요! 저는 컴비서입니다.", "tool_calls": []}])
    old = agentmod.llm
    agentmod.llm = fake
    try:
        evs = drive(agentmod.run_agent(CFG, [{"role": "user", "content": "자기소개"}],
                                       session_id="", checkpoint=lambda s: ck.append(s)))
        types = [e.get("type") for e in evs]
        ok("status 이벤트 발생", "status" in types)
        ok("final 이벤트 발생", types[-1] == "final")
        ok("체크포인트 호출됨", len(ck) >= 1)
        phases = [c.get("phase") for c in ck]
        ok("체크포인트에 단계 기록", "생각 중" in phases)
        sys_msgs = [m for c in ck if c.get("messages")
                    for m in runs.trim_messages(c["messages"]) if m.get("role") == "system"]
        ok("저장 시 시스템 메시지 제거", sys_msgs == [])
        ok("체크포인트에 대화 내용 포함",
           any(any(m.get("role") == "user" for m in (c.get("messages") or [])) for c in ck))

        sec("도구 호출 이력 보존 + 재개")
        fake = FakeLLM([{"text": "완료했습니다.", "tool_calls": []}])
        agentmod.llm = fake
        hist = [
            {"role": "user", "content": "파일 읽어줘"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c9", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"hello.txt"}'}}]},
            {"role": "tool", "tool_call_id": "c9", "name": "read_file", "content": "안녕 컴비서"},
        ]
        evs = drive(agentmod.run_agent(CFG, hist, session_id=""))
        ok("재개 final", any(e.get("type") == "final" for e in evs))
        sent = fake.seen[0] if fake.seen else []
        ok("assistant tool_calls 보존",
           any(m.get("role") == "assistant" and m.get("tool_calls") for m in sent))
        ok("tool 결과 보존",
           any(m.get("role") == "tool" and m.get("tool_call_id") == "c9" for m in sent))

        sec("끊긴 tool_call 재개 (repair 후 실행)")
        fixed = runs.repair_messages(hist[:2])       # tool 결과 없이 assistant 까지만
        fake = FakeLLM([{"text": "다시 시도해 완료했습니다.", "tool_calls": []}])
        agentmod.llm = fake
        evs = drive(agentmod.run_agent(CFG, fixed, session_id=""))
        ok("끊김 재개 final", any(e.get("type") == "final" for e in evs))
        sent = fake.seen[0] if fake.seen else []
        ok("가짜 tool 결과가 전달됨",
           any(m.get("role") == "tool" for m in sent))
    finally:
        agentmod.llm = old

    sec("세션 저장/복구 연동")
    s = sessions.new_session()
    s["messages"].append({"role": "user", "content": "질문", "ts": 1})
    sessions.save(s)
    r4 = runs.start(s["id"], "build")
    runs.save_state(r4["id"], messages=[{"role": "user", "content": "질문"}])
    runs.finish(r4["id"], "interrupted", "crash")
    loaded = sessions.load(s["id"])
    ok("세션 파일 유지", loaded and loaded["messages"][0]["content"] == "질문")
    ok("재개 대상 탐색", (runs.active_for_session(s["id"]) or {}).get("status") == "interrupted")

    print("\n" + "=" * 64)
    print(f"결과:  통과 {PASS}  /  실패 {FAIL}")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
