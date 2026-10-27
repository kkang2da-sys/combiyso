# -*- coding: utf-8 -*-
"""실행 저널(체크포인트).

작업(에이전트 실행) 중 상태를 **매 단계 디스크에 저장**해 두어,
프로그램이 갑자기 죽어도(뻗어도) 다음에 이어서 계속할 수 있게 한다.

저장 위치: %APPDATA%\\compbiyso\\runs\\<run_id>.json (원자적 쓰기)

레코드 구조
{
  "id": "run_20261018-120000-ab12cd",
  "session_id": "...", "agent": "build",
  "status": "running|interrupted|error|stopped|done",
  "step": 3, "phase": "도구 실행", "detail": "read_file",
  "started": <ts>, "updated": <ts>, "finished": <ts|null>,
  "error": "", "note": "",
  "todos": [...],
  "stats": {"tools": 5},
  "state": {"messages": [ ... 재개용 메시지(시스템 제외) ... ]}
}
"""
import json
import os
import time
import uuid
from pathlib import Path

from .config import data_dir

RUNS_DIR = data_dir() / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

# 이어서 할 수 있는(=state 가 남아 있는) 상태
RESUMABLE = ("interrupted", "error", "stopped")

_MSG_CONTENT_CAP = 8000        # 저장 시 메시지 하나의 최대 길이
_STATE_MAX_CHARS = 2_000_000   # 저널 하나의 최대 크기(문자)
_PRUNE_KEEP = 200


def _safe(s):
    return "".join(ch for ch in str(s or "") if ch.isalnum() or ch in "-_")


def _path(rid):
    return RUNS_DIR / f"{_safe(rid)}.json"


def new_id():
    return "run_" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load(rid):
    p = _path(rid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(rec):
    try:
        _atomic_write(_path(rec["id"]), json.dumps(rec, ensure_ascii=False))
    except Exception:
        pass
    return rec


# --------------------------------------------------------------------------- #
def trim_messages(messages):
    """저장용으로 메시지 목록을 적당히 줄인다(용량 제한)."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        item = {}
        for k, v in m.items():
            if k == "content":
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                item[k] = s[:_MSG_CONTENT_CAP]
            else:
                item[k] = v
        out.append(item)
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in out)
    while out and total > _STATE_MAX_CHARS:
        removed = out.pop(0)
        total -= len(json.dumps(removed, ensure_ascii=False))
    return out


def start(session_id, agent="build", note=""):
    now = time.time()
    rec = {
        "id": new_id(), "session_id": _safe(session_id), "agent": agent,
        "status": "running", "step": 0, "phase": "시작", "detail": "",
        "started": now, "updated": now, "finished": None, "error": "", "note": note,
        "todos": [], "stats": {"tools": 0}, "state": {"messages": []},
    }
    return _save(rec)


def save_state(rid, status=None, step=None, phase=None, detail=None,
               todos=None, messages=None, stats=None, error=None):
    rec = _load(rid)
    if not rec:
        return None
    if status:
        rec["status"] = status
    if step is not None:
        rec["step"] = step
    if phase is not None:
        rec["phase"] = phase
    if detail is not None:
        rec["detail"] = detail
    if todos is not None:
        rec["todos"] = todos
    if error is not None:
        rec["error"] = error
    if stats is not None:
        rec["stats"] = stats
    if messages is not None:
        rec.setdefault("state", {})["messages"] = trim_messages(messages)
    rec["updated"] = time.time()
    return _save(rec)


def finish(rid, status="done", error=""):
    rec = _load(rid)
    if not rec:
        return None
    rec["status"] = status
    rec["error"] = error or rec.get("error", "")
    rec["finished"] = time.time()
    rec["updated"] = rec["finished"]
    if status == "done":
        rec["state"] = {"messages": []}     # 완료 → 재개 불필요, 용량 회수
    return _save(rec)


def get(rid):
    return _load(rid)


def list_runs(active_only=True):
    out = []
    if not RUNS_DIR.is_dir():
        return out
    for p in RUNS_DIR.glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if active_only and rec.get("status") not in (("running",) + RESUMABLE):
            continue
        out.append(rec)
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return out


def active_for_session(sid):
    sid = _safe(sid)
    best = None
    for rec in list_runs(active_only=True):
        if rec.get("session_id") == sid:
            if best is None or rec.get("updated", 0) > best.get("updated", 0):
                best = rec
    return best


def discard(rid):
    p = _path(rid)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception:
            return False
    return False


def sweep_startup():
    """프로그램 시작 시: 살아 있을 수 없는 'running' 을 'interrupted' 로 표시."""
    n = 0
    for rec in list_runs(active_only=True):
        if rec.get("status") == "running":
            rec["status"] = "interrupted"
            note = rec.get("note") or ""
            if "정상 종료" not in note:
                rec["note"] = (note + " (프로그램이 정상 종료되지 않았습니다)").strip()
            rec["updated"] = time.time()
            _save(rec)
            n += 1
    return n


def prune(keep=_PRUNE_KEEP):
    recs = []
    if not RUNS_DIR.is_dir():
        return
    for p in RUNS_DIR.glob("*.json"):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    recs.sort(key=lambda x: x.get("updated", 0), reverse=True)
    for rec in recs[keep:]:
        discard(rec.get("id"))


def repair_messages(messages):
    """재개 전 정리: 응답 없는 tool_call 에 안내용 가짜 결과를 채워 API 오류를 막는다."""
    msgs = [dict(m) for m in (messages or []) if isinstance(m, dict)]
    answered = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    out = []
    for m in msgs:
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                cid = tc.get("id") if isinstance(tc, dict) else None
                if cid and cid not in answered:
                    out.append({
                        "role": "tool", "tool_call_id": cid,
                        "name": (tc.get("function") or {}).get("name", ""),
                        "content": "(이전 실행이 중단되어 결과가 없습니다. 다시 시도하세요.)",
                    })
                    answered.add(cid)
    if out and out[-1].get("role") == "assistant" and \
            "<tool" in str(out[-1].get("content", "")).lower():
        out.append({"role": "user",
                    "content": "(이전 도구 실행이 중단되었습니다. 결과가 없으니 다시 시도하세요.)"})
    return out


def mem_free_mb():
    """사용 가능한 물리 메모리(MB). 실패하면 None."""
    try:
        from .config import mem_info
        _total, avail = mem_info()
        return avail or None
    except Exception:
        return None
