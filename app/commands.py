# -*- coding: utf-8 -*-
"""커스텀 명령 (/명령어) 관리.

opencode 처럼 Markdown 파일 하나가 명령 하나가 된다.

  %APPDATA%\\compbiyso\\commands\\<이름>.md
  ---
  description: 설명
  ---
  본문 … $ARGUMENTS … $1 $2

내장 명령은 kind 로 구분한다.
  prompt : 본문을 프롬프트로 펼쳐서 모델에게 보냄
  local  : 서버가 직접 처리(모델 호출 없음)
  ui     : UI 가 처리
"""
import re
from pathlib import Path

from .config import COMMANDS_DIR

ID_RE = re.compile(r"^[^\s/\\:*?\"<>|]{1,64}$")
_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", re.S)


BUILTINS = [
    {
        "id": "init", "description": "작업 폴더를 분석해 COMPIYSO.md 규칙 초안을 만듭니다",
        "kind": "prompt", "builtin": True,
        "body": (
            "이 작업 폴더를 분석해서 `COMPIYSO.md` 규칙 파일 초안을 작성해줘.\n"
            "1) `list_dir`(recursive=true) 로 구조를 보고, README·설정 파일 등 핵심 파일을 읽어라.\n"
            "2) 프로젝트 성격(언어/빌드/테스트/코딩 규칙)을 파악해라.\n"
            "3) 아래 항목을 포함한 간결한 한국어 Markdown 을 만들어 `COMPIYSO.md` 로 저장해라.\n"
            "   - 프로젝트 한 줄 설명\n   - 폴더 구조 요약\n   - 빌드/실행/테스트 명령\n"
            "   - 코드 작성 규칙(네이밍·주석·커밋 메시지)\n   - 하면 안 되는 일\n"
            "4) 저장 전에 사용자에게 미리 보여줘라.\n"
            "추가 지시: $ARGUMENTS"
        ),
    },
    {
        "id": "compact", "description": "지금 대화를 요약해 컨텍스트를 압축합니다",
        "kind": "local", "builtin": True, "body": "",
    },
    {
        "id": "undo", "description": "최근에 에이전트가 수정한 파일을 되돌립니다",
        "kind": "local", "builtin": True, "body": "",
    },
    {
        "id": "help", "description": "사용 가능한 명령과 스킬 목록을 봅니다",
        "kind": "local", "builtin": True, "body": "",
    },
    {
        "id": "skills", "description": "사용 가능한 스킬 목록을 봅니다",
        "kind": "local", "builtin": True, "body": "",
    },
    {
        "id": "tools", "description": "사용 가능한 도구 목록을 봅니다",
        "kind": "local", "builtin": True, "body": "",
    },
]


def valid_id(cid: str) -> bool:
    return bool(cid) and bool(ID_RE.match(str(cid).strip())) and str(cid).strip() not in (".", "..")


def command_path(cid: str) -> Path:
    return COMMANDS_DIR / f"{str(cid).strip()}.md"


def _parse(text: str):
    text = text or ""
    m = _FM_RE.match(text)
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


def _builtin(cid: str):
    return next((c for c in BUILTINS if c["id"] == cid), None)


def get_command(cid: str):
    cid = str(cid or "").strip()
    b = _builtin(cid)
    if b:
        return dict(b)
    p = command_path(cid)
    if not p.is_file():
        return None
    try:
        meta, body = _parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return {"id": cid, "name": meta.get("name") or cid,
            "description": meta.get("description") or "",
            "body": body.strip(), "argument_hint": meta.get("argument-hint", ""),
            "kind": "prompt", "builtin": False, "path": str(p)}


def list_commands():
    out = [dict(c) for c in BUILTINS]
    if COMMANDS_DIR.is_dir():
        for p in sorted(COMMANDS_DIR.glob("*.md"), key=lambda x: x.name.lower()):
            c = get_command(p.stem)
            if c:
                out.append(c)
    # 내장이 먼저, 그 뒤 사용자 정의
    return out


def save_command(cid: str, description: str = "", body: str = "") -> dict:
    cid = str(cid).strip()
    if not valid_id(cid):
        raise ValueError("명령 이름은 공백/특수문자 없이 64자 이하로 정하세요.")
    if _builtin(cid):
        raise ValueError(f"'{cid}' 는 내장 명령이라 수정할 수 없습니다.")
    text = f"---\ndescription: {(description or '').strip()}\n---\n\n{(body or '').strip()}\n"
    command_path(cid).write_text(text, encoding="utf-8")
    return get_command(cid)


def delete_command(cid: str) -> bool:
    cid = str(cid).strip()
    if _builtin(cid) or not valid_id(cid):
        return False
    p = command_path(cid)
    if p.is_file():
        p.unlink()
        return True
    return False


def render(body: str, args: str) -> str:
    args = args or ""
    parts = args.split()
    out = (body or "").replace("$ARGUMENTS", args)
    for i in range(1, 10):
        out = out.replace(f"${i}", parts[i - 1] if i <= len(parts) else "")
    return out.strip()


def parse_slash(text: str):
    """'/이름 나머지' → (이름, 나머지). 아니면 None."""
    t = (text or "").strip()
    if not t.startswith("/") or t.startswith("//"):
        return None
    m = re.match(r"^/([^\s/]+)\s*(.*)$", t, re.S)
    if not m:
        return None
    return m.group(1), (m.group(2) or "").strip()


def expand(text: str, skills=None):
    """슬래시 입력을 해석한다.

    반환: {"kind": "prompt|local|skill|ui", "id":..., "prompt":..., "args":...} 또는 None
    """
    parsed = parse_slash(text)
    if not parsed:
        return None
    name, args = parsed

    # 1) 스킬 직접 호출 (/스킬id)
    for s in (skills or []):
        if s.get("id") == name:
            return {"kind": "skill", "id": name, "args": args,
                    "prompt": (f"`skill` 도구로 '{name}' 스킬을 불러와서 그 지침대로 작업해줘.\n"
                               f"사용자 요청: {args or '(스킬 지침에 맞는 작업을 알아서 수행)'}\n"
                               f"작업이 끝나면 무엇을 했는지 한국어로 보고해줘.")}

    # 2) 명령
    c = get_command(name)
    if not c:
        return None
    kind = c.get("kind") or "prompt"
    if kind == "prompt":
        return {"kind": "prompt", "id": name, "args": args,
                "prompt": render(c.get("body") or "", args) or (args or name)}
    return {"kind": kind, "id": name, "args": args, "prompt": "", "command": c}
