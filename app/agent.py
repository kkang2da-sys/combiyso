# -*- coding: utf-8 -*-
"""에이전트 루프: LLM ↔ 도구 실행 반복.

run_agent() 는 이벤트(dict)를 순서대로 내보낸다.
  {"type":"delta","text":...}                 최종 답변 스트리밍 조각
  {"type":"reset"}                            지금까지 보낸 조각 취소
  {"type":"tool","id":"t1","name":...,"args":{}}          도구 호출 시작
  {"type":"tool_result","id":"t1","name":...,"ok":bool,"result":str,
        "diff":str,"path":str,"backup":str}
  {"type":"approval","id":"ap_...","kind":...,"title":...,"detail":...,"extra":{}}
  {"type":"approval_result","id":"ap_...","decision":"allow|deny"}
  {"type":"todos","todos":[{text,status}]}
  {"type":"compact","info":{...}}
  {"type":"usage","tokens":n,"limit":n,"percent":n}
  {"type":"subagent","sub_id":..,"status":"start|end","prompt":..,"text":..,"agent":..}
  {"type":"final","text":str,"trace":[...],"todos":[...]}
  {"type":"error","message":str}
"""
import json
import queue as _queue
import re
import threading
import time
from pathlib import Path

from . import agents as agentsmod
from . import context, llm, mcp, perf, permissions, vcs
from .approval import BROKER
from .tools import TOOL_SPECS, AgentCtx, Tools

TOOL_TAG_RE = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.S | re.IGNORECASE)
STOP_FLAGS = set()

# 파일/시스템을 바꿀 수 있는 권한 종류 → 실행 전에 git 체크포인트를 뜬다
MUTATING_KINDS = {"write", "delete", "move", "mkdir", "copy", "shell"}


# --------------------------------------------------------------------------- #
def request_stop(session_id):
    if session_id:
        STOP_FLAGS.add(session_id)
        BROKER.cancel_all()


def clear_stop(session_id):
    STOP_FLAGS.discard(session_id)


def is_stopped(session_id):
    return bool(session_id) and session_id in STOP_FLAGS


# --------------------------------------------------------------------------- #
def _use_native(cfg):
    mode = cfg.get("tool_mode") or "auto"
    if mode == "native":
        return True
    if mode == "text":
        return False
    return (cfg.get("provider") or "ollama") == "openai"


def _allows(allowed, name: str) -> bool:
    if not allowed or allowed == "*":
        return True
    if name in allowed:
        return True
    if name.startswith("mcp__"):
        return any(a in allowed for a in ("mcp", "mcp__", "mcp__*"))
    return False


def mcp_specs(allowed="*"):
    specs = []
    for full, server, t in mcp.MANAGER.all_tools():
        if not _allows(allowed, full):
            continue
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        specs.append({"type": "function", "function": {
            "name": full,
            "description": f"[MCP/{server}] " + str(t.get("description") or "")[:400],
            "parameters": schema,
        }})
    return specs


def all_specs(cfg, depth=0, allowed="*"):
    specs = list(TOOL_SPECS)
    if depth > 0:
        specs = [s for s in specs if s["function"]["name"] != "task"]
    else:
        specs += mcp_specs(allowed)
    if allowed and allowed != "*":
        specs = [s for s in specs if _allows(allowed, s["function"]["name"])]
    return specs


def load_rules(cfg) -> str:
    if not cfg.get("use_rules_file", True):
        return ""
    name = cfg.get("rules_file") or "COMPIYSO.md"
    try:
        p = Path(str(cfg.get("workspace") or "")) / name
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")[:20000]
    except Exception:
        pass
    return ""


def build_system(cfg, specs, native, depth=0, todos=None, agent_def=None, skills=None):
    ws = cfg.get("workspace") or ""
    agent_def = agent_def or {}
    body = str(agent_def.get("body") or "").strip()

    role = ("당신은 \"컴비서\"라는 이름의 한국어 AI 에이전트입니다. "
            "사용자의 컴퓨터에서 파일 작업과 명령 실행을 도와줍니다.")
    if depth > 0:
        role = ("당신은 \"컴비서\"의 하위 에이전트입니다. 주어진 조사/작업을 수행하고 "
                "결과를 간결한 한국어 보고로 돌려주세요.")

    lines = [role, f"현재 에이전트: {agent_def.get('id', 'build')}"]
    if body:
        lines += ["", body]
    lines += [
        f"작업 폴더(워크스페이스): {ws}",
        "항상 한국어로 답하세요. 필요한 것만 짧고 정확하게.",
        "",
        "사용할 수 있는 도구:",
    ]
    for spec in specs:
        f = spec["function"]
        props = ", ".join((f.get("parameters") or {}).get("properties", {}).keys())
        lines.append(f'- {f["name"]}({props}): {f["description"]}')

    if native:
        lines += ["", "필요하면 도구를 호출하세요. 도구가 필요 없으면 바로 답하세요."]
    else:
        lines += [
            "",
            "도구를 사용할 때는 아래 형식으로 정확히 출력하세요(설명 없이 도구 태그만):",
            '<tool>{"name": "도구이름", "args": {"인자": "값"}}</tool>',
            "여러 개가 필요하면 <tool> 태그를 여러 번 출력해도 됩니다.",
            "도구 결과는 <result>...</result> 로 주어집니다.",
            "더 이상 도구가 필요 없으면 <tool> 태그 없이 최종 답변만 작성하세요.",
            "한 번의 답변에 도구 태그와 일반 답변을 섞지 마세요.",
        ]

    if skills:
        lines += ["", "사용 가능한 스킬(작업에 맞으면 `skill` 도구로 전체 지침을 불러오세요):"]
        for s in skills:
            lines.append(f"- {s.get('id')}: {s.get('name')} — {(s.get('description') or '')[:200]}")

    lines += [
        "",
        "작업 원칙:",
        "1) 파일을 고치기 전에 먼저 read_file 로 현재 내용을 확인하세요.",
        "2) 수정은 edit_file(부분 교체)을 우선 쓰고, 전체 재작성은 정말 필요할 때만 write_file.",
        "3) 큰 작업(3단계 이상)은 todo_write 로 계획을 세우고, 진행하며 상태를 갱신하세요.",
        "4) 결과는 사용자가 바로 이해하도록 정리해서 보고하세요.",
    ]
    if depth == 0:
        lines.append("5) 폴더를 통째로 조사하는 등 긴 탐색은 task 도구(하위 에이전트)에 맡기면 좋습니다.")
        if parallel_enabled(cfg):
            lines.append("5-1) 서로 독립적인 조사를 2개 이상 해야 하면 task 도구의 tasks 배열로 "
                         "한 번에 동시에 맡기세요(훨씬 빠릅니다). 쓰기가 필요한 작업은 병렬로 "
                         "넘기지 말고 하나씩 순서대로 처리하세요.")
    lines.append("6) 쓰기/삭제/실행은 사용자 권한 규칙에 따라 허용·승인요청·차단될 수 있습니다.")

    rules = load_rules(cfg)
    if rules:
        lines += ["", "=== 작업 폴더 규칙 파일 (반드시 따르세요) ===", rules, "=== 규칙 끝 ==="]

    if todos:
        lines += ["", "현재 할 일 목록:"]
        for t in todos:
            mark = {"done": "[x]", "in_progress": "[>]"}.get(t.get("status"), "[ ]")
            lines.append(f"{mark} {t.get('text')}")

    if cfg.get("system_extra"):
        lines += ["", "추가 지시:", str(cfg["system_extra"])]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _calls_from_text(text):
    calls = []
    for m in TOOL_TAG_RE.finditer(text or ""):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        name = obj.get("name") or obj.get("tool")
        if name:
            calls.append({"id": None, "name": name,
                          "args": obj.get("args") or obj.get("arguments") or {}})
    return calls


def _strip_tags(text):
    return TOOL_TAG_RE.sub("", text or "").strip()


# --------------------------------------------------------------------------- #
# 하위 에이전트 (병렬)
# --------------------------------------------------------------------------- #
def resolve_subagent(cfg, want):
    """(읽기전용 여부, agent_id, 정의) — 존재하지 않으면 general 로 폴백."""
    sub_def = agentsmod.get_agent(want or cfg.get("subagent_type") or "general")
    if sub_def.get("mode") not in ("subagent", "all"):
        sub_def = agentsmod.get_agent("general")
    aid = sub_def.get("id", "general")
    allow = sub_def.get("tools") or "*"
    readonly = (allow != "*"
                and not any(t in allow for t in agentsmod.WRITE_TOOLS)
                and "mcp" not in allow)
    return readonly, aid, sub_def


def parallel_enabled(cfg) -> bool:
    cfg = cfg or {}
    mode = str(cfg.get("subagent_parallel") or "auto").lower()
    if mode == "off":
        return False
    if cfg.get("_force_sequential"):
        return False
    try:
        if int(perf.GOV.update().get("pressure") or 0) >= 2:
            return False              # 메모리가 부족하면 동시 LLM 호출 금지
    except Exception:
        pass
    if mode == "on":
        return True
    return not cfg.get("_low_spec_active")      # auto: 저사양이면 순차


def _run_subagents_parallel(cfg, session_id, depth, items):
    """여러 하위 에이전트를 동시에 실행하며 이벤트를 흘려보내는 제너레이터.

    마지막에 {"__results__": {sub_id: text}} 를 한 번 내보낸다.
    (스레드는 데몬이라 중지/이탈 시 프로세스를 붙잡지 않는다.)
    """
    q = _queue.Queue()
    results, errors = {}, {}

    def worker(it):
        try:
            for sev in run_agent(cfg, [{"role": "user", "content": it["prompt"]}],
                                 session_id=session_id, depth=depth + 1,
                                 agent_id=it["agent"]):
                t = sev.get("type")
                if t == "final":
                    results[it["sub_id"]] = sev.get("text", "")
                    continue
                if t in ("delta", "reset", "usage", "compact", "todos", "status"):
                    continue
                sev = dict(sev)
                sev["sub_id"] = it["sub_id"]
                sev["sub"] = True
                if t in ("tool", "tool_result"):
                    q.put(sev)
                elif t == "error":
                    errors[it["sub_id"]] = (errors.get(it["sub_id"], "") + "\n" +
                                            str(sev.get("message"))).strip()
        except Exception as e:
            errors[it["sub_id"]] = f"{type(e).__name__}: {e}"
        finally:
            q.put({"__done__": it["sub_id"]})

    for it in items:
        threading.Thread(target=worker, args=(it,), daemon=True).start()

    pending = {it["sub_id"] for it in items}
    while pending:
        try:
            ev = q.get(timeout=0.5)
        except _queue.Empty:
            if is_stopped(session_id):
                break
            continue
        if "__done__" in ev:
            sid_ = ev["__done__"]
            pending.discard(sid_)
            yield {"type": "subagent", "sub_id": sid_, "status": "end",
                   "text": results.get(sid_, "") or errors.get(sid_, ""),
                   "agent": "", "parallel": True}
            continue
        yield ev

    yield {"__results__": {it["sub_id"]: (results.get(it["sub_id"])
                                          or errors.get(it["sub_id"]) or "")
                           for it in items}}


# --------------------------------------------------------------------------- #
def run_agent(cfg, history, session_id="", depth=0, agent_id=None, todos=None,
              checkpoint=None):
    """이벤트 제너레이터.

    checkpoint: 선택 콜백. 상태를 디스크에 저장할 때 호출된다(중단 복구용).
    """
    base_cfg = cfg
    if base_cfg.get("memory_guard", True):
        cfg = perf.tune(base_cfg)          # 시작 시점 메모리 압박 반영
    agent_def = agentsmod.get_agent(agent_id or cfg.get("agent") or "build")
    allowed = agent_def.get("tools") or "*"
    native = _use_native(cfg)
    specs = all_specs(cfg, depth, allowed)
    max_steps = max(1, int(cfg.get("max_steps") or 40))
    if depth > 0:
        max_steps = max(1, int(cfg.get("subagent_max_steps") or 20))

    def make_call_cfg(c):
        if agent_def.get("temperature") is not None:
            d = dict(c)
            d["temperature"] = agent_def["temperature"]
            return d
        return c

    call_cfg = make_call_cfg(cfg)

    denied_kinds = {k for k, v in (cfg.get("permission_actions") or {}).items() if v == "deny"}
    ctx = AgentCtx(cfg, session_id=session_id)
    ctx.todos = [dict(t) for t in (todos or []) if isinstance(t, dict)]
    tools = Tools(cfg, ctx, allowed_tools=(None if allowed == "*" else allowed),
                  denied_kinds=denied_kinds)

    messages = [{"role": "system", "content": ""}]
    for m in history or []:
        if not isinstance(m, dict):
            continue
        item = {"role": m.get("role", "user"), "content": m.get("content", "")}
        for k in ("tool_calls", "tool_call_id", "name"):
            if m.get(k) is not None:
                item[k] = m[k]
        if m.get("images"):
            item["images"] = list(m["images"])[:6]      # 첨부 이미지(ollama/openai 공통)
        messages.append(item)

    trace = []
    tool_seq = 0
    todos = list(ctx.todos)
    ckpt_state = {"done": False}
    warned_pressure = -1

    def _maybe_checkpoint(tool_name):
        """이번 실행에서 처음으로 파일을 바꾸기 직전, git 스냅샷 1회."""
        if ckpt_state["done"]:
            return None
        if permissions.tool_kind(tool_name) not in MUTATING_KINDS:
            return None
        ckpt_state["done"] = True
        try:
            info = vcs.snapshot(cfg, label=f"실행 전 · {agent_def.get('id', 'build')} "
                                           f"{time.strftime('%m-%d %H:%M')}")
        except Exception as e:
            return {"type": "checkpoint", "ok": False,
                    "reason": f"{type(e).__name__}: {e}"}
        if not info:
            return None
        return {"type": "checkpoint", **info}

    def _ck(status="running", step=None, phase=None, detail="", stats=None):
        if not checkpoint:
            return
        try:
            checkpoint({"status": status, "step": step, "phase": phase,
                        "detail": detail, "todos": todos, "messages": messages,
                        "stats": stats or {"tools": tool_seq}})
        except Exception:
            pass

    for _step in range(max_steps):
        if is_stopped(session_id):
            _ck(status="stopped", step=_step, phase="사용자가 중지함")
            yield {"type": "final", "text": "(사용자가 중지했습니다.)", "trace": trace,
                   "todos": todos}
            return

        # --- 런타임 메모리 거버너 (v1.5.0) ---
        live = {"pressure": 0, "avail_mb": 0}
        if base_cfg.get("memory_guard", True):
            try:
                live = perf.GOV.update()
                cfg = perf.tune(base_cfg)
                call_cfg = make_call_cfg(cfg)
                tools.cfg = cfg
                try:
                    tools.max_out = int(cfg.get("max_tool_output") or tools.max_out)
                except Exception:
                    pass
                p = int(live.get("pressure") or 0)
                if p >= 2 and p > warned_pressure:
                    warned_pressure = p
                    yield {"type": "note",
                           "text": (f"메모리가 부족합니다(여유 {live.get('avail_mb')}MB). "
                                    "도구 출력과 컨텍스트를 자동으로 줄여 계속 진행합니다.")}
            except Exception:
                pass

        # 압박이 심하면 남은 단계 수도 즉시 줄인다(루프 범위는 시작 시 고정이므로 직접 확인).
        try:
            cap = int(cfg.get("max_steps") or max_steps)
            if cap > 0:
                max_steps = min(max_steps, cap)
        except Exception:
            pass
        if _step >= max_steps:
            _ck(status="running", step=_step, phase="메모리 절약을 위해 단계 제한")
            break

        yield {"type": "status", "phase": "생각 중", "detail": "",
               "step": _step + 1, "depth": depth}
        _ck(step=_step + 1, phase="생각 중")

        messages[0]["content"] = build_system(cfg, specs, native, depth, todos,
                                              agent_def, tools.skills)
        # --- 컨텍스트 자동 압축 ---
        try:
            new_msgs, info = context.maybe_compact(
                cfg, messages, lambda ms: llm.complete(call_cfg, ms))
            if info:
                messages = new_msgs
                yield {"type": "compact", "info": info}
        except Exception:
            pass

        # --- LLM 호출 ---
        text, calls = "", []
        try:
            if native:
                tool_calls = []
                parts = []
                for ev in llm.chat_stream(call_cfg, messages, tools=specs):
                    if "delta" in ev:
                        parts.append(ev["delta"])
                        yield {"type": "delta", "text": ev["delta"]}
                    if ev.get("final"):
                        text = ev.get("text") or "".join(parts)
                        tool_calls = ev.get("tool_calls") or []
                if not text:
                    text = "".join(parts)
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function") or {}
                    calls.append({"id": tc.get("id") or f"call_{i}",
                                  "name": fn.get("name", ""),
                                  "args": _parse_args(fn.get("arguments"))})
            else:
                suppressed = False
                tail = ""
                parts = []
                for ev in llm.chat_stream(call_cfg, messages, tools=None):
                    if "delta" in ev:
                        parts.append(ev["delta"])
                        tail = (tail + ev["delta"])[-32:]
                        if not suppressed:
                            if "<tool" in tail.lower():
                                suppressed = True
                                yield {"type": "reset"}
                            else:
                                yield {"type": "delta", "text": ev["delta"]}
                    if ev.get("final"):
                        text = ev.get("text") or "".join(parts)
                buf = text or "".join(parts)
                text = buf
                calls = _calls_from_text(text)
        except MemoryError:
            perf.safe_gc()
            msg = ("메모리가 부족해 모델 응답을 받지 못했습니다. "
                   "대화를 새로 시작하거나 설정 → 성능에서 모델을 내린 뒤 다시 시도하세요.")
            yield {"type": "error", "message": msg}
            yield {"type": "final", "text": "⚠ " + msg, "trace": trace, "todos": todos}
            return
        except Exception as e:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
            yield {"type": "final", "text": f"⚠ 오류가 발생했습니다: {type(e).__name__}: {e}",
                   "trace": trace, "todos": todos}
            return

        if native and not calls:
            calls = _calls_from_text(text)

        if not calls:
            yield {"type": "final", "text": _strip_tags(text) or "(빈 응답)",
                   "trace": trace, "todos": todos}
            return

        if text.strip():
            yield {"type": "reset"}

        if native:
            messages.append({
                "role": "assistant",
                "content": _strip_tags(text),
                "tool_calls": [{"id": c["id"] or f"call_{i}", "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                               for i, c in enumerate(calls)],
            })
        else:
            messages.append({"role": "assistant", "content": text})

        results = []
        for c in calls:
            tool_seq += 1
            tid = f"t{tool_seq}"
            name, args = c["name"], (c["args"] or {})
            call_id = c["id"] or f"call_{tool_seq}"

            yield {"type": "status", "phase": "도구 준비", "detail": name,
                   "step": _step + 1, "depth": depth}
            _ck(step=_step + 1, phase="도구 준비", detail=name)

            # ---------------- 하위 에이전트 (단일/병렬) ----------------
            if name == "task" and depth == 0:
                spec_list = []
                raw = args.get("tasks")
                if isinstance(raw, list):
                    for t in raw[:8]:
                        if isinstance(t, dict) and str(t.get("prompt") or "").strip():
                            spec_list.append({
                                "prompt": str(t["prompt"]).strip(),
                                "want": str(t.get("subagent_type") or "").strip()})
                single = str(args.get("prompt") or "").strip()
                if single:
                    spec_list.insert(0, {"prompt": single,
                                         "want": str(args.get("subagent_type") or "").strip()})
                if not spec_list:
                    res = "task 도구에는 prompt 또는 tasks 가 필요합니다."
                    yield {"type": "tool", "id": tid, "name": "task", "args": args}
                    yield {"type": "tool_result", "id": tid, "name": "task", "ok": False,
                           "result": res, "diff": "", "path": "", "backup": ""}
                    if native:
                        messages.append({"role": "tool", "tool_call_id": call_id,
                                         "name": "task", "content": res})
                    else:
                        results.append(f'<result name="task">\n{res}\n</result>')
                    continue

                for it in spec_list:
                    ro, aid_, _ = resolve_subagent(cfg, it["want"])
                    it["agent"], it["readonly"] = aid_, ro

                max_conc = max(2, int(cfg.get("subagent_parallel_max") or 3))
                use_parallel = (parallel_enabled(cfg) and len(spec_list) > 1
                                and len(spec_list) <= max_conc
                                and all(it["readonly"] for it in spec_list))

                yield {"type": "tool", "id": tid, "name": "task", "args": args}
                sub_results = {}
                if use_parallel:
                    items = [{"sub_id": "sub%d_%d" % (tool_seq, k),
                              "prompt": it["prompt"], "agent": it["agent"]}
                             for k, it in enumerate(spec_list)]
                    yield {"type": "parallel", "count": len(items),
                           "agents": [it["agent"] for it in items]}
                    for it in items:
                        yield {"type": "subagent", "sub_id": it["sub_id"], "status": "start",
                               "prompt": it["prompt"], "agent": it["agent"], "parallel": True}
                    for pev in _run_subagents_parallel(cfg, session_id, depth, items):
                        if "__results__" in pev:
                            sub_results = pev["__results__"]
                            continue
                        yield pev
                else:
                    if len(spec_list) > 1:
                        yield {"type": "note",
                               "text": "여러 작업을 순차 실행합니다(병렬 불가: 쓰기 도구 사용 또는 설정 꺼짐)."}
                    for k, it in enumerate(spec_list):
                        sub_id = "sub%d_%d" % (tool_seq, k)
                        yield {"type": "subagent", "sub_id": sub_id, "status": "start",
                               "prompt": it["prompt"], "agent": it["agent"],
                               "parallel": False}
                        sub_final = ""
                        try:
                            for sev in run_agent(cfg, [{"role": "user", "content": it["prompt"]}],
                                                 session_id=session_id, depth=depth + 1,
                                                 agent_id=it["agent"]):
                                t = sev.get("type")
                                if t == "final":
                                    sub_final = sev.get("text", "")
                                    continue
                                if t in ("delta", "reset", "usage", "compact", "todos",
                                         "status"):
                                    continue
                                sev = dict(sev)
                                sev["sub_id"] = sub_id
                                sev["sub"] = True
                                if t in ("tool", "tool_result"):
                                    yield sev
                                elif t == "error":
                                    sub_final = (sub_final + "\n" +
                                                 str(sev.get("message"))).strip()
                        except Exception as e:
                            sub_final = f"(하위 에이전트 오류: {type(e).__name__}: {e})"
                        yield {"type": "subagent", "sub_id": sub_id, "status": "end",
                               "text": sub_final, "agent": it["agent"]}
                        sub_results[sub_id] = sub_final

                if len(spec_list) == 1:
                    res = sub_results.get("sub%d_0" % tool_seq) or "(하위 에이전트 결과 없음)"
                else:
                    blocks = []
                    for k, it in enumerate(spec_list):
                        blocks.append("### %d) %s — %s\n%s" % (
                            k + 1, it["agent"], it["prompt"][:70],
                            sub_results.get("sub%d_%d" % (tool_seq, k)) or "(결과 없음)"))
                    res = "\n\n".join(blocks)
                trace.append({"name": "task/" + "+".join(it["agent"] for it in spec_list),
                              "args": args, "ok": True, "result": res[:800],
                              "parallel": use_parallel})
                yield {"type": "tool_result", "id": tid, "name": "task", "ok": True,
                       "result": res, "diff": "", "path": "", "backup": "",
                       "parallel": use_parallel}
                if native:
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "name": "task", "content": res})
                else:
                    results.append(f'<result name="task">\n{res}\n</result>')
                _ck(step=_step + 1, phase="하위 에이전트 완료",
                    detail="+".join(it["agent"] for it in spec_list))
                continue

            # ---------------- 권한 판정 ----------------
            kind = permissions.tool_kind(name)
            target = permissions.target_of(tools, name, args)
            perm = permissions.resolve(cfg, name, target, kind)

            if perm["action"] == "deny":
                res = (f"⛔ 권한 규칙에 의해 차단되었습니다 ({perm['reason']}).\n"
                       f"대상: {target or name}")
                yield {"type": "tool", "id": tid, "name": name, "args": args}
                yield {"type": "tool_result", "id": tid, "name": name, "ok": False,
                       "result": res, "diff": "", "path": "", "backup": "",
                       "blocked": True, "reason": perm["reason"]}
                trace.append({"name": name, "args": args, "ok": False, "result": res[:400]})
                if native:
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "name": name, "content": res})
                else:
                    results.append(f'<result name="{name}">\n{res}\n</result>')
                _ck(step=_step + 1, phase="차단됨", detail=name)
                continue

            if perm["action"] == "ask" and not BROKER.is_session_allowed(session_id, kind or name):
                info = tools.approval_info(name, args) or {
                    "kind": kind or name, "title": name, "detail": target,
                    "extra": {"tool": name, "args": args}}
                aid = BROKER.create(info["kind"], info["title"], info["detail"],
                                    info["extra"], sid=session_id)
                yield {"type": "approval", "id": aid, "tool_id": tid, "name": name,
                       "kind": info["kind"], "title": info["title"],
                       "detail": info["detail"], "extra": info["extra"],
                       "reason": perm["reason"]}
                yield {"type": "status", "phase": "승인 대기", "detail": info["title"],
                       "step": _step + 1, "depth": depth}
                _ck(step=_step + 1, phase="승인 대기", detail=info["title"])
                decision = BROKER.wait(aid, timeout=900)
                yield {"type": "approval_result", "id": aid, "decision": decision}
                if decision != "allow":
                    res = "사용자가 이 작업을 거부했습니다. 다른 방법을 제안하세요."
                    yield {"type": "tool_result", "id": tid, "name": name, "ok": False,
                           "result": res, "diff": "", "path": "", "backup": ""}
                    trace.append({"name": name, "args": args, "ok": False, "result": res})
                    if native:
                        messages.append({"role": "tool", "tool_call_id": call_id,
                                         "name": name, "content": res})
                    else:
                        results.append(f'<result name="{name}">\n{res}\n</result>')
                    _ck(step=_step + 1, phase="거부됨", detail=name)
                    continue

            # ---------------- 실행 ----------------
            ck_ev = _maybe_checkpoint(name)          # 파일을 바꾸기 전 1회 스냅샷
            if ck_ev:
                yield ck_ev
                if ck_ev.get("ok"):
                    _ck(step=_step + 1, phase="되돌리기 지점 저장", detail="git 체크포인트")
            yield {"type": "tool", "id": tid, "name": name, "args": args}
            extra, res, ok = {}, "", True
            try:
                if name.startswith("mcp__"):
                    res = mcp.MANAGER.call(name, args)
                else:
                    res, extra = tools.run(name, args)
            except MemoryError:
                perf.safe_gc()
                res = ("메모리 부족으로 도구를 실행하지 못했습니다. "
                       "더 작은 범위로 다시 시도하세요.")
                ok = False
            except Exception as e:
                res = f"오류: {type(e).__name__}: {e}"
                ok = False

            yield {"type": "tool_result", "id": tid, "name": name, "ok": ok,
                   "result": str(res), "diff": extra.get("diff", ""),
                   "path": extra.get("path", ""), "backup": extra.get("backup", "")}
            trace.append({"name": name, "args": args, "ok": ok, "result": str(res)[:800]})

            if name == "todo_write":
                todos = [dict(t) for t in ctx.todos]
                yield {"type": "todos", "todos": todos}

            if native:
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "name": name, "content": str(res)})
            else:
                results.append(f'<result name="{name}">\n{res}\n</result>')
            _ck(step=_step + 1, phase="도구 완료", detail=name)

        if not native:
            messages.append({"role": "user", "content": "\n".join(results)})
            _ck(step=_step + 1, phase="도구 결과 반영", detail="")

        yield {"type": "usage", **context.usage_info(messages, cfg)}

    yield {"type": "final",
           "text": f"(최대 단계 수 {max_steps}에 도달했습니다. 계속하려면 다시 요청해 주세요.)",
           "trace": trace, "todos": todos}
