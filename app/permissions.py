# -*- coding: utf-8 -*-
"""세밀 권한 규칙.

도구 호출마다 ① 사용자 규칙(도구/패턴) → ② 종류별 기본 동작 순서로
allow / ask / deny 를 결정한다.

규칙 예:
  {"tool": "run_command", "pattern": "git push*",  "action": "ask"}
  {"tool": "run_command", "pattern": "rm -rf*",    "action": "deny"}
  {"tool": "read_file",   "pattern": "*.env",      "action": "deny"}
  {"tool": "write_file",  "pattern": "C:\\Windows\\*", "action": "deny"}
  {"tool": "mcp",         "pattern": "*",          "action": "allow"}
"""
import fnmatch

KIND_OF = {
    "list_dir": "read", "read_file": "read", "glob_files": "read", "grep": "read",
    "write_file": "write", "edit_file": "write", "multi_edit": "write",
    "delete_file": "delete", "move_file": "move", "copy_file": "copy",
    "make_dir": "mkdir", "run_command": "shell",
    "fetch_url": "network", "web_search": "network",
}

NO_PERMISSION = {"todo_write", "todoread", "skill", "task"}

KIND_ORDER = ["read", "write", "delete", "move", "copy", "mkdir", "shell", "network", "mcp"]
KIND_LABEL = {
    "read": "읽기/검색", "write": "파일 쓰기·수정", "delete": "파일 삭제",
    "move": "이동/이름변경", "copy": "복사", "mkdir": "폴더 생성",
    "shell": "명령 실행", "network": "네트워크", "mcp": "MCP 도구",
}
ACTION_LABEL = {"allow": "허용", "ask": "물어보기", "deny": "차단"}

PRESETS = {
    "ask": {"read": "allow", "write": "ask", "delete": "ask", "move": "ask",
            "copy": "allow", "mkdir": "allow", "shell": "ask", "network": "allow",
            "mcp": "ask"},
    "auto": {k: "allow" for k in KIND_ORDER},
    "readonly": {"read": "allow", "write": "deny", "delete": "deny", "move": "deny",
                 "copy": "deny", "mkdir": "deny", "shell": "deny", "network": "allow",
                 "mcp": "deny"},
}


def tool_kind(name: str):
    name = str(name or "")
    if name in NO_PERMISSION:
        return None
    if name.startswith("mcp__"):
        return "mcp"
    return KIND_OF.get(name)


def _norm(s: str) -> str:
    return str(s or "").replace("/", "\\").strip().lower()


def _match(rule: dict, tool: str, kind: str, target: str) -> bool:
    t = str(rule.get("tool") or "*").strip().lower()
    if t not in ("*", "all", "전체", ""):
        if t != str(tool).lower() and t != str(kind or "").lower():
            return False
    pat = str(rule.get("pattern") or "*").strip()
    if pat and pat not in ("*", "전체"):
        tgt = str(target or "")
        if not fnmatch.fnmatch(_norm(tgt), _norm(pat)):
            if not fnmatch.fnmatch(tgt, pat):
                return False
    return True


def target_of(tools, name: str, args: dict) -> str:
    args = args or {}
    if name == "run_command":
        return str(args.get("command") or "")
    if name == "fetch_url":
        return str(args.get("url") or "")
    if name == "web_search":
        return str(args.get("query") or "")
    if name in ("copy_file", "move_file"):
        return f"{args.get('src')} -> {args.get('dst')}"
    p = args.get("path") or args.get("file") or args.get("dir") or args.get("target")
    if p:
        if tools is not None:
            try:
                return str(tools._resolve(p))
            except Exception:
                return str(p)
        return str(p)
    for key in ("pattern", "query", "text", "content"):
        if key in args and args[key]:
            return str(args[key])[:300]
    return ""


def resolve(cfg: dict, tool: str, target: str, kind: str = None) -> dict:
    """{"action": allow|ask|deny, "kind":..., "reason":...}"""
    if kind is None:
        kind = tool_kind(tool)
    if kind is None:
        return {"action": "allow", "kind": None, "reason": "권한 불필요", "target": target}

    for rule in (cfg.get("permission_rules") or []):
        if isinstance(rule, dict) and _match(rule, tool, kind, target):
            a = str(rule.get("action") or "ask").lower()
            if a not in ("allow", "ask", "deny"):
                a = "ask"
            return {"action": a, "kind": kind, "target": target, "rule": rule,
                    "reason": f"규칙 {rule.get('tool', '*')}/{rule.get('pattern', '*')} → "
                              f"{ACTION_LABEL[a]}"}

    actions = cfg.get("permission_actions") or {}
    d = str(actions.get(kind) or "ask").lower()
    if d not in ("allow", "ask", "deny"):
        d = "ask"
    return {"action": d, "kind": kind, "target": target,
            "reason": f"{KIND_LABEL.get(kind, kind)} 기본 → {ACTION_LABEL[d]}"}


def action_for_kind(cfg: dict, kind: str) -> str:
    d = str((cfg.get("permission_actions") or {}).get(kind) or "ask").lower()
    return d if d in ("allow", "ask", "deny") else "ask"
