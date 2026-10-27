# -*- coding: utf-8 -*-
"""에이전트 정의 (opencode 의 agent 와 같은 개념).

  %APPDATA%\\compbiyso\\agents\\<id>.md
  ---
  description: 설명
  mode: primary | subagent | all
  tools: list_dir, read_file, grep     (또는 *)
  temperature: 0.2
  ---
  본문 = 그 에이전트의 시스템 프롬프트

내장 에이전트(build / plan / general / explore)는 파일로 덮어쓸 수 있다.
"""
import re
from pathlib import Path

from .config import AGENTS_DIR

ID_RE = re.compile(r"^[^\s/\\:*?\"<>|]{1,64}$")
_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", re.S)

READ_TOOLS = ["list_dir", "read_file", "glob_files", "grep", "fetch_url",
              "web_search", "todoread", "todo_write", "skill", "task"]
WRITE_TOOLS = ["write_file", "edit_file", "multi_edit", "make_dir", "copy_file",
               "move_file", "delete_file", "run_command"]

BUILTIN = {
    "build": {
        "id": "build", "name": "build",
        "description": "기본 에이전트 — 모든 도구로 실제 작업을 수행",
        "mode": "primary", "tools": "*", "temperature": None, "body": "",
        "builtin": True,
    },
    "plan": {
        "id": "plan", "name": "plan",
        "description": "계획 모드 — 읽기/검색만. 파일 수정·명령 실행 불가",
        "mode": "primary", "tools": list(READ_TOOLS), "temperature": None,
        "body": (
            "당신은 **계획(plan) 모드**입니다.\n"
            "- 파일을 수정하거나 명령을 실행할 수 없습니다(도구가 제공되지 않습니다).\n"
            "- 조사·분석·설계만 하고, 실행할 단계를 구체적으로 제안하세요.\n"
            "- 최종 답변은 '무엇을 / 어떤 파일에 / 어떤 순서로' 할지 목록으로 정리하세요.\n"
            "- 실제 실행이 필요하면 사용자에게 build 모드로 바꾸라고 안내하세요."
        ),
        "builtin": True,
    },
    "general": {
        "id": "general", "name": "general",
        "description": "범용 하위 에이전트 — 여러 단계 작업을 대신 수행",
        "mode": "subagent", "tools": "*", "temperature": None, "body": "",
        "builtin": True,
    },
    "explore": {
        "id": "explore", "name": "explore",
        "description": "탐색 전용 하위 에이전트 — 폴더 구조·코드 검색에 특화(읽기 전용)",
        "mode": "subagent", "tools": list(READ_TOOLS), "temperature": 0.1,
        "body": (
            "당신은 코드베이스 탐색 전문 하위 에이전트입니다.\n"
            "- `list_dir`, `glob_files`, `grep`, `read_file` 을 적극적으로 사용하세요.\n"
            "- 파일을 수정하지 마세요. 오직 조사만 합니다.\n"
            "- 답변은 '질문에 대한 결론 → 근거(파일:줄) → 관련 파일 목록' 순서로 간결하게."
        ),
        "builtin": True,
    },
}


def valid_id(aid: str) -> bool:
    return bool(aid) and bool(ID_RE.match(str(aid).strip())) and str(aid).strip() not in (".", "..")


def agent_path(aid: str) -> Path:
    return AGENTS_DIR / f"{str(aid).strip()}.md"


def _parse(text: str):
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, m.group(2)


def parse_tools(value):
    if value is None:
        return "*"
    if isinstance(value, list):
        return value
    v = str(value).strip()
    if v in ("*", "all", "전체"):
        return "*"
    return [x.strip() for x in v.split(",") if x.strip()]


def _load_file(aid: str):
    p = agent_path(aid)
    if not p.is_file():
        return None
    try:
        meta, body = _parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    base = dict(BUILTIN.get(aid) or {})
    base.update({
        "id": aid,
        "name": meta.get("name") or aid,
        "description": meta.get("description") or base.get("description", ""),
        "mode": (meta.get("mode") or base.get("mode") or "primary").strip().lower(),
        "tools": parse_tools(meta.get("tools") if "tools" in meta else base.get("tools", "*")),
        "body": body.strip(),
        "path": str(p),
        "builtin": False,
    })
    try:
        t = meta.get("temperature")
        base["temperature"] = float(t) if t not in (None, "") else base.get("temperature")
    except Exception:
        pass
    if base["mode"] not in ("primary", "subagent", "all"):
        base["mode"] = "primary"
    return base


def get_agent(aid: str):
    aid = str(aid or "").strip()
    if not aid:
        return dict(BUILTIN["build"])
    f = _load_file(aid)
    if f:
        return f
    if aid in BUILTIN:
        return dict(BUILTIN[aid])
    return dict(BUILTIN["build"])


def list_agents():
    out = []
    seen = set()
    if AGENTS_DIR.is_dir():
        for p in sorted(AGENTS_DIR.glob("*.md"), key=lambda x: x.name.lower()):
            a = _load_file(p.stem)
            if a:
                out.append(a)
                seen.add(a["id"])
    for aid, a in BUILTIN.items():
        if aid not in seen:
            out.append(dict(a))
    return out


def primary_agents():
    return [a for a in list_agents() if a.get("mode") in ("primary", "all")]


def subagents():
    return [a for a in list_agents() if a.get("mode") in ("subagent", "all")]


def save_agent(aid: str, description: str = "", mode: str = "primary",
               tools="*", body: str = "", temperature=None) -> dict:
    aid = str(aid).strip()
    if not valid_id(aid):
        raise ValueError("에이전트 id 는 공백/특수문자 없이 64자 이하로 정하세요.")
    mode = (mode or "primary").strip().lower()
    if mode not in ("primary", "subagent", "all"):
        mode = "primary"
    tools_v = parse_tools(tools)
    tools_str = "*" if tools_v == "*" else ", ".join(tools_v)
    lines = ["---", f"description: {(description or '').strip()}", f"mode: {mode}",
             f"tools: {tools_str}"]
    if temperature not in (None, ""):
        lines.append(f"temperature: {temperature}")
    lines.append("---")
    text = "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"
    agent_path(aid).write_text(text, encoding="utf-8")
    return get_agent(aid)


def delete_agent(aid: str) -> bool:
    aid = str(aid).strip()
    if not valid_id(aid):
        return False
    p = agent_path(aid)
    if p.is_file():
        p.unlink()
        return True
    return False


def is_builtin_only(aid: str) -> bool:
    return aid in BUILTIN and not agent_path(aid).is_file()
