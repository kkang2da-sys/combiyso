# -*- coding: utf-8 -*-
"""컴비서 도구 모음.

안전장치
- 모든 경로는 워크스페이스 안으로 제한(설정에서 해제 가능)
- 권한 표(permission_actions) + 세밀 규칙(permission_rules)로 allow/ask/deny
- 에이전트별 도구 허용목록
- 위험 명령 차단
- 수정/삭제 전 자동 백업(되돌리기 가능)
"""
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import backup, permissions, skills as skillsmod, websearch


class ToolError(Exception):
    pass


class AgentCtx:
    """도구 실행 중 공유되는 상태(에이전트가 만들어 넘긴다)."""

    def __init__(self, cfg: dict, subagent_runner=None, session_id: str = ""):
        self.cfg = cfg
        self.todos = []
        self.subagent_runner = subagent_runner
        self.session_id = session_id


# --------------------------------------------------------------------------- #
# 도구 스펙 (LLM 에게 보여줄 스키마)
# --------------------------------------------------------------------------- #
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "폴더 안의 파일/하위폴더 목록을 봅니다.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "폴더 경로(워크스페이스 기준 상대경로 가능, 기본 '.')"},
            "recursive": {"type": "boolean", "description": "하위 폴더까지 모두 표시(기본 false)"},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "텍스트 파일을 줄번호와 함께 읽습니다.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "파일 경로"},
            "start": {"type": "integer", "description": "시작 줄(기본 1)"},
            "end": {"type": "integer", "description": "끝 줄(기본 끝까지)"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "파일을 새로 쓰거나 전체 내용을 덮어씁니다.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "파일 경로"},
            "content": {"type": "string", "description": "저장할 전체 내용"},
        }, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "파일에서 old_string 을 new_string 으로 바꿉니다.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "파일 경로"},
            "old_string": {"type": "string", "description": "바꿀 기존 문자열"},
            "new_string": {"type": "string", "description": "새 문자열"},
            "replace_all": {"type": "boolean", "description": "모두 바꾸기(기본 false: 유일해야 함)"},
        }, "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "function": {
        "name": "multi_edit",
        "description": "한 파일에 여러 곳을 한 번에 수정합니다.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "파일 경로"},
            "edits": {"type": "array", "description": "[{old_string, new_string, replace_all?}] 목록",
                      "items": {"type": "object", "properties": {
                          "old_string": {"type": "string"}, "new_string": {"type": "string"},
                          "replace_all": {"type": "boolean"}}}},
        }, "required": ["path", "edits"]}}},
    {"type": "function", "function": {
        "name": "glob_files",
        "description": "이름 패턴으로 파일을 찾습니다(예: *.py, report*.txt).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "파일명 패턴"},
            "path": {"type": "string", "description": "찾을 폴더(기본 '.')"},
        }, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "정규식으로 파일 내용을 검색합니다.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "정규식 패턴"},
            "path": {"type": "string", "description": "검색할 폴더/파일(기본 '.')"},
            "glob": {"type": "string", "description": "파일명 필터(예: '*.py')"},
            "ignore_case": {"type": "boolean", "description": "대소문자 무시"},
            "context": {"type": "integer", "description": "앞뒤 문맥 줄 수(기본 0)"},
            "files_only": {"type": "boolean", "description": "파일 경로만 표시"},
        }, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "셸 명령을 실행하고 출력을 반환합니다.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "실행할 명령"},
            "cwd": {"type": "string", "description": "실행 폴더(기본 워크스페이스)"},
            "timeout": {"type": "integer", "description": "제한 시간(초)"},
        }, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "make_dir",
        "description": "폴더를 만듭니다(하위 폴더 포함).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "만들 폴더 경로"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "copy_file",
        "description": "파일이나 폴더를 복사합니다.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string", "description": "원본 경로"},
            "dst": {"type": "string", "description": "대상 경로"},
            "overwrite": {"type": "boolean", "description": "덮어쓰기 허용(기본 false)"},
        }, "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "move_file",
        "description": "파일이나 폴더를 이동/이름변경합니다.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string", "description": "원본 경로"},
            "dst": {"type": "string", "description": "대상 경로"},
        }, "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "delete_file",
        "description": "파일을 삭제합니다(백업 저장됨).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "삭제할 경로"},
            "recursive": {"type": "boolean", "description": "폴더면 하위까지 삭제(기본 false)"},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "웹 페이지/텍스트를 가져와 본문 텍스트로 반환합니다.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "주소"},
            "max_chars": {"type": "integer", "description": "최대 글자수(기본 20000)"},
        }, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "인터넷에서 검색합니다(제목/주소/요약).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "검색어"},
            "max_results": {"type": "integer", "description": "결과 개수(기본 5)"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "todo_write",
        "description": "여러 단계 작업의 할 일 목록을 기록/갱신합니다.",
        "parameters": {"type": "object", "properties": {
            "todos": {"type": "array", "description": "[{text, status}] (status: pending|in_progress|done)",
                      "items": {"type": "object", "properties": {
                          "text": {"type": "string"}, "status": {"type": "string"}}}},
        }, "required": ["todos"]}}},
    {"type": "function", "function": {
        "name": "todoread",
        "description": "지금까지 기록한 할 일 목록을 다시 읽습니다.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "skill",
        "description": ("필요할 때 스킬(전문 작업 절차)의 전체 지침을 불러옵니다. "
                        "시스템 프롬프트에 이름과 설명만 나열되어 있으니, 해당 작업을 할 때 호출하세요."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "스킬 id"},
            "resource": {"type": "string", "description": "스킬 폴더 안의 참고 파일(선택)"},
        }, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "task",
        "description": ("복잡한 조사를 별도의 하위 에이전트에게 맡기고 결과만 받습니다(컨텍스트 절약). "
                        "독립적인 조사 여러 개는 tasks 배열로 한 번에 **동시에** 시킬 수 있습니다"
                        "(읽기 전용 에이전트만 병렬 실행)."),
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "하위 에이전트에게 줄 지시(단일)"},
            "subagent_type": {"type": "string",
                              "description": "사용할 하위 에이전트 id (기본 general, 탐색은 explore)"},
            "tasks": {"type": "array", "description": "동시에 실행할 작업들(2개 이상이면 병렬)",
                      "items": {"type": "object", "properties": {
                          "prompt": {"type": "string"},
                          "subagent_type": {"type": "string"}},
                          "required": ["prompt"]}},
        }, "required": []}}},
]

BLOCKED_COMMANDS = (
    "format c:", "rm -rf /", "del /f /s /q c:\\", "del /q c:\\", "rd /s /q c:\\",
    "shutdown", "diskpart", "mkfs", "cipher /w", "vssadmin delete",
    ":(){:|:&};:",
)

# 승인이 필요한 도구 → (종류, 한국어 이름)
APPROVAL_TARGETS = {
    "write_file": "write", "edit_file": "write", "multi_edit": "write",
    "delete_file": "delete", "move_file": "move", "copy_file": "copy",
    "make_dir": "mkdir", "run_command": "shell",
}

APPROVAL_LABEL = {
    "read": "읽기/검색", "write": "파일 쓰기/수정", "delete": "파일 삭제",
    "move": "이동/이름변경", "copy": "복사", "mkdir": "폴더 생성",
    "shell": "명령 실행", "network": "네트워크", "mcp": "MCP 도구",
}


def _truncate(text, n):
    """도구 출력 자르기 — 앞부분(맥락)과 뒷부분(오류/결과)을 함께 남긴다."""
    text = str(text)
    if n and n > 0 and len(text) > n:
        head = max(1, int(n * 0.65))
        tail = max(1, n - head)
        return (text[:head]
                + f"\n… (중략: 전체 {len(text):,}자 · 앞 {head:,}자 + 뒤 {tail:,}자 표시)\n"
                + text[-tail:])
    return text


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def unified_diff(old: str, new: str, fromfile: str, tofile: str, limit: int = 400) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=fromfile,
                                     tofile=tofile, lineterm="", n=2))
    if not diff:
        return ""
    if len(diff) > limit:
        diff = diff[:limit] + [f"… (diff 생략: 전체 {len(diff)}줄)"]
    return "\n".join(diff)


class Tools:
    def __init__(self, cfg: dict, ctx: AgentCtx = None,
                 allowed_tools=None, denied_kinds=None):
        self.cfg = cfg
        self.ctx = ctx or AgentCtx(cfg)
        self.workspace = Path(str(cfg.get("workspace") or Path.home())).expanduser().resolve()
        self.max_out = int(cfg.get("max_tool_output") or 20000)
        # 에이전트 도구 허용목록 (None = 전체)
        self.allowed = set(allowed_tools) if allowed_tools else None
        # 권한 표에서 '차단'으로 지정된 종류
        self.denied_kinds = set(denied_kinds or [])
        # 사용 가능한 스킬(점진적 공개: 이름+설명만 프롬프트에 노출)
        self.skills = []
        if cfg.get("skills_enabled", True):
            disabled = set(cfg.get("disabled_skills") or [])
            try:
                self.skills = [s for s in skillsmod.list_skills() if s.get("id") not in disabled]
            except Exception:
                self.skills = []

    def tool_allowed(self, name: str) -> bool:
        if self.allowed is None:
            return True
        name = str(name or "")
        if name in self.allowed:
            return True
        if name.startswith("mcp__"):
            return any(a in self.allowed for a in ("mcp", "mcp__*", "mcp__"))
        return False

    def _guard_kind(self, name: str):
        kind = permissions.tool_kind(name)
        if kind and kind in self.denied_kinds:
            raise ToolError(
                f"'{permissions.KIND_LABEL.get(kind, kind)}' 권한이 차단으로 설정되어 있어 "
                f"실행할 수 없습니다(설정 → 권한).")

    # ------------------------------------------------------------------ 경로
    def _resolve(self, raw, must_exist=False):
        p = Path(str(raw or ".")).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        try:
            p = p.resolve()
        except Exception:
            p = Path(os.path.abspath(str(p)))
        if not self.cfg.get("allow_outside_workspace"):
            ws = str(self.workspace).lower()
            low = str(p).lower()
            if not (low == ws or low.startswith(ws + os.sep)):
                raise ToolError(f"워크스페이스 밖은 접근할 수 없습니다: {p}\n(허용: {self.workspace})")
        if must_exist and not p.exists():
            raise ToolError(f"경로가 없습니다: {p}")
        return p

    # -------------------------------------------------------------- 승인 정보
    def approval_info(self, name, args):
        kind = permissions.tool_kind(name)
        if not kind or name in ("list_dir", "read_file", "glob_files", "grep",
                                "fetch_url", "web_search"):
            return None
        args = args or {}
        detail = ""
        if name == "run_command":
            detail = str(args.get("command") or "")
        elif name in ("copy_file", "move_file"):
            detail = f"{args.get('src')}  →  {args.get('dst')}"
        elif name == "multi_edit":
            edits = args.get("edits") or []
            detail = f"{args.get('path')}  (수정 {len(edits)}곳)"
        elif name == "write_file":
            content = str(args.get("content") or "")
            detail = f"{args.get('path')}  (새 내용 {len(content)}자)"
        elif name == "edit_file":
            detail = (f"{args.get('path')}\n- {str(args.get('old_string'))[:200]}"
                      f"\n+ {str(args.get('new_string'))[:200]}")
        elif name.startswith("mcp__"):
            detail = f"{name}\n" + str(args)[:600]
            detail = detail.strip()
        else:
            detail = str(args.get("path") or args.get("src") or "")
        return {"kind": kind, "title": APPROVAL_LABEL.get(kind, permissions.KIND_LABEL.get(kind, kind)),
                "detail": detail, "extra": {"tool": name, "args": args}}

    # -------------------------------------------------------------- 공통 점검
    def _backup(self, path, reason="edit"):
        if not self.cfg.get("auto_backup", True):
            return ""
        bid = backup.backup(path, reason)
        if bid:
            backup.prune(int(self.cfg.get("backup_keep") or 300))
        return bid

    # ---------------------------------------------------------------- 도구들
    def list_dir(self, path=".", recursive=False):
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"경로가 없습니다: {p}")
        if p.is_file():
            return f"{p.name} (파일, {_human(p.stat().st_size)})"
        rows = []
        if recursive:
            # 저사양 보호: 수만 개 파일을 한 번에 메모리에 올리지 않는다
            items = []
            for child in p.rglob("*"):
                items.append(child)
                if len(items) >= 3000:
                    break
            items.sort(key=lambda x: str(x).lower())
        else:
            items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        for child in items[:1000]:
            try:
                rel = child.relative_to(p)
                size = _human(child.stat().st_size) if child.is_file() else ""
            except Exception:
                rel, size = child.name, ""
            rows.append(f"{'[D]' if child.is_dir() else '[F]'} {rel}{'/' if child.is_dir() else ''}  {size}")
        body = "\n".join(rows) if rows else "(빈 폴더)"
        return _truncate(f"{p}\n{body}", self.max_out)

    def read_file(self, path, start=1, end=None):
        p = self._resolve(path, must_exist=True)
        if not p.is_file():
            raise ToolError(f"파일이 아닙니다: {p}")
        raw = p.read_bytes()[:600_000]
        if b"\x00" in raw[:4000]:
            return f"{p} — 바이너리 파일로 보입니다({_human(p.stat().st_size)}). 텍스트가 아닙니다."
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        s = max(1, int(start or 1))
        e = int(end) if end else len(lines)
        e = min(max(e, s), len(lines))
        body = "\n".join(f"{i:>5}: {lines[i - 1]}" for i in range(s, e + 1))
        return _truncate(f"{p} (전체 {len(lines)}줄, {s}-{e} 표시)\n{body}", self.max_out)

    def write_file(self, path, content):
        p = self._resolve(path)
        content = str(content)
        old = ""
        if p.exists() and p.is_file():
            self._backup(p, "write")
            old = p.read_text(encoding="utf-8", errors="replace")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        diff = unified_diff(old, content, str(p), str(p)) if old else ""
        return {"text": f"저장 완료: {p} ({len(content)}자, {_human(p.stat().st_size)})",
                "diff": diff, "path": str(p)}

    def edit_file(self, path, old_string, new_string, replace_all=False):
        p = self._resolve(path, must_exist=True)
        text = p.read_text(encoding="utf-8", errors="replace")
        old_string, new_string = str(old_string), str(new_string)
        n = text.count(old_string)
        if n == 0:
            raise ToolError("old_string 을 찾지 못했습니다. 파일 내용을 다시 확인하세요.")
        if n > 1 and not replace_all:
            raise ToolError(f"old_string 이 {n}번 나타납니다. 문맥을 더 넣거나 replace_all=true 를 쓰세요.")
        self._backup(p, "edit")
        new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
        p.write_text(new_text, encoding="utf-8")
        return {"text": f"수정 완료: {p} ({n if replace_all else 1}곳)",
                "diff": unified_diff(text, new_text, str(p), str(p)), "path": str(p)}

    def multi_edit(self, path, edits):
        p = self._resolve(path, must_exist=True)
        text = p.read_text(encoding="utf-8", errors="replace")
        original = text
        applied = 0
        for i, ed in enumerate(edits or []):
            old, new = str(ed.get("old_string", "")), str(ed.get("new_string", ""))
            all_ = bool(ed.get("replace_all"))
            if not old:
                continue
            cnt = text.count(old)
            if cnt == 0:
                raise ToolError(f"{i + 1}번째 수정: old_string 을 찾지 못했습니다.")
            if cnt > 1 and not all_:
                raise ToolError(f"{i + 1}번째 수정: {cnt}번 나타납니다(문맥 추가 또는 replace_all).")
            text = text.replace(old, new, -1 if all_ else 1)
            applied += 1
        if applied == 0:
            raise ToolError("적용할 수정이 없습니다.")
        self._backup(p, "multi_edit")
        p.write_text(text, encoding="utf-8")
        return {"text": f"수정 완료: {p} ({applied}개 편집)",
                "diff": unified_diff(original, text, str(p), str(p)), "path": str(p)}

    def glob_files(self, pattern, path="."):
        p = self._resolve(path)
        if not p.is_dir():
            raise ToolError(f"폴더가 아닙니다: {p}")
        pat = str(pattern or "*")
        out = []
        if any(ch in pat for ch in ("/", "\\")):
            for f in p.glob(pat):
                out.append(str(f))
                if len(out) >= 5000:
                    break
        else:
            for f in p.rglob("*"):
                if fnmatch.fnmatch(f.name, pat):
                    out.append(str(f))
                    if len(out) >= 5000:
                        break
        out = sorted(out)
        if not out:
            return f"'{pat}' 에 해당하는 파일이 없습니다. ({p})"
        return _truncate(f"'{pat}' 결과 {len(out)}개:\n" + "\n".join(out[:400]), self.max_out)

    def grep(self, pattern, path=".", glob=None, ignore_case=False, context=0, files_only=False):
        p = self._resolve(path)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(str(pattern), flags)
        except re.error as e:
            raise ToolError(f"정규식 오류: {e}")
        if p.is_file():
            files = [p]
        else:
            files = []
            for f in p.rglob("*"):          # 저사양 보호: 최대 8000개까지만
                if f.is_file():
                    files.append(f)
                    if len(files) >= 8000:
                        break
        ctxn = max(0, int(context or 0))
        out = []
        matched_files = 0
        for f in files:
            if glob and not fnmatch.fnmatch(f.name, str(glob)):
                continue
            try:
                if f.stat().st_size > 3_000_000:
                    continue
                raw = f.read_bytes()
                if b"\x00" in raw[:2000]:
                    continue
                text = raw.decode("utf-8", errors="ignore")
            except Exception:
                continue
            lines = text.splitlines()
            hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
            if not hits:
                continue
            matched_files += 1
            if files_only:
                out.append(str(f))
            else:
                for i in hits[:40]:
                    if ctxn:
                        lo, hi = max(0, i - ctxn), min(len(lines), i + ctxn + 1)
                        out.append(f"{f}:{i + 1}")
                        for j in range(lo, hi):
                            mark = ">" if j == i else " "
                            out.append(f"{mark} {j + 1:>5}: {lines[j][:220]}")
                    else:
                        out.append(f"{f}:{i + 1}: {lines[i].strip()[:220]}")
            if len(out) >= 300:
                break
        if not out:
            return "(일치 없음)"
        head = f"파일 {matched_files}개에서 발견:\n" if not files_only else f"파일 {matched_files}개:\n"
        return _truncate(head + "\n".join(out), self.max_out)

    def run_command(self, command, cwd=None, timeout=None):
        cwd_p = self._resolve(cwd or ".")
        if not cwd_p.is_dir():
            cwd_p = self.workspace
        low = str(command).lower()
        for bad in BLOCKED_COMMANDS:
            if bad in low:
                raise ToolError(f"차단된 명령입니다: {bad}")
        tmo = int(timeout or self.cfg.get("command_timeout") or 120)
        t0 = time.time()
        kw = {}
        if os.name == "nt":
            # 콘솔 창이 깜빡이며 뜨는 것을 막고, 출력 인코딩 사고를 줄인다
            kw["creationflags"] = 0x08000000            # CREATE_NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            r = subprocess.run(str(command), shell=True, cwd=str(cwd_p),
                               capture_output=True, timeout=tmo, env=env, **kw)
        except subprocess.TimeoutExpired:
            return f"시간 초과({tmo}s): {command}"
        except OSError as e:
            return f"실행 실패: {type(e).__name__}: {e}\n$ {command}"
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        err = (r.stderr or b"").decode("utf-8", errors="replace")
        dur = time.time() - t0
        return _truncate(f"$ {command}\n(exit={r.returncode}, {dur:.1f}s)\n"
                         f"--- stdout ---\n{out}\n--- stderr ---\n{err}", self.max_out)

    def make_dir(self, path):
        p = self._resolve(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"폴더 생성: {p}"

    def copy_file(self, src, dst, overwrite=False):
        s = self._resolve(src, must_exist=True)
        d = self._resolve(dst)
        if d.exists() and not overwrite:
            raise ToolError(f"대상이 이미 있습니다(overwrite=true 로 덮어쓰기 가능): {d}")
        if d.exists():
            self._backup(d, "copy-overwrite")
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(str(s), str(d), dirs_exist_ok=True)
        else:
            shutil.copy2(str(s), str(d))
        return f"복사 완료: {s} → {d}"

    def move_file(self, src, dst):
        s = self._resolve(src, must_exist=True)
        d = self._resolve(dst)
        if s.is_file():
            self._backup(s, "move")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return f"이동 완료: {s} → {d}"

    def delete_file(self, path, recursive=False):
        p = self._resolve(path, must_exist=True)
        if p == self.workspace:
            raise ToolError("워크스페이스 폴더 자체는 삭제할 수 없습니다.")
        if p.is_dir():
            if not recursive:
                raise ToolError("폴더입니다. recursive=true 를 주세요.")
            n_files = 0
            for f in p.rglob("*"):
                if f.is_file():
                    self._backup(f, "delete")
                    n_files += 1
            shutil.rmtree(str(p))
            return f"폴더 삭제: {p} (파일 {n_files}개 백업됨)"
        bid = self._backup(p, "delete")
        p.unlink()
        return f"파일 삭제: {p}" + (f" (백업 {bid} — 되돌리기 가능)" if bid else "")

    def fetch_url(self, url, max_chars=20000):
        r = websearch.fetch_text(url, max_chars=int(max_chars or 20000),
                                 timeout=int(self.cfg.get("web_timeout") or 25))
        if not r.get("ok"):
            return f"가져오기 실패: {r.get('error')}\n({r.get('url')})"
        head = f"{r.get('title') or '(제목 없음)'}\n{r.get('url')}"
        return _truncate(f"{head}\n\n{r.get('content')}", self.max_out)

    def web_search(self, query, max_results=None):
        n = int(max_results or self.cfg.get("web_max_results") or 5)
        results = websearch.search(query, max_results=n,
                                   timeout=int(self.cfg.get("web_timeout") or 20))
        return _truncate(websearch.format_results(results), self.max_out)

    def todoread(self):
        todos = self.ctx.todos or []
        if not todos:
            return "(기록된 할 일 없음)"
        mark = {"done": "[x]", "in_progress": "[>]"}
        lines = [f"{mark.get(t.get('status'), '[ ]')} {t.get('text')}" for t in todos]
        done = sum(1 for t in todos if t.get("status") == "done")
        return f"할 일 {len(todos)}개 (완료 {done}):\n" + "\n".join(lines)

    def skill(self, name, resource=None):
        sid = str(name or "").strip()
        if not sid:
            raise ToolError("스킬 이름(name)이 필요합니다.")
        info = skillsmod.get_skill(sid)
        if not info:
            avail = ", ".join(s.get("id", "") for s in self.skills) or "없음"
            raise ToolError(f"'{sid}' 스킬을 찾을 수 없습니다. 사용 가능: {avail}")
        if resource:
            body = skillsmod.read_resource(sid, str(resource))
            return _truncate(f"[스킬 {sid} / 리소스 {resource}]\n\n{body}", self.max_out)
        res = info.get("resources") or []
        res_txt = ("\n\n참고 리소스(필요하면 resource 인자로 읽으세요):\n" +
                   "\n".join("- " + r["path"] for r in res)) if res else ""
        return _truncate(f"[스킬 {sid}: {info.get('name')}]\n{info.get('description')}\n\n"
                         f"{info.get('body')}{res_txt}", self.max_out)

    def task(self, prompt, subagent_type=None):
        if not self.ctx.subagent_runner:
            raise ToolError("하위 에이전트를 사용할 수 없습니다.")
        return str(self.ctx.subagent_runner(str(prompt), subagent_type))

    # ------------------------------------------------------------- 디스패치
    def run(self, name, args):
        """(text, extra) 반환. extra = {diff, path}"""
        name = str(name or "")
        if not self.tool_allowed(name):
            raise ToolError(f"이 에이전트는 '{name}' 도구를 사용할 수 없습니다.")
        self._guard_kind(name)
        fn = getattr(self, name, None)
        blocked = ("run", "dispatch", "_resolve", "_backup", "approval_info",
                   "tool_allowed", "_guard_kind", "cfg", "ctx", "workspace",
                   "max_out", "allowed", "denied_kinds", "skills")
        if not callable(fn) or name.startswith("_") or name in blocked:
            raise ToolError(f"알 수 없는 도구입니다: {name}")
        result = fn(**(args or {}))
        if isinstance(result, dict):
            return result.get("text", ""), {k: v for k, v in result.items() if k != "text"}
        return str(result), {}

    def todo_write(self, todos):
        items = []
        for t in (todos or []):
            if isinstance(t, str):
                t = {"text": t, "status": "pending"}
            if not isinstance(t, dict):
                continue
            status = str(t.get("status") or "pending").lower()
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            text = str(t.get("text") or t.get("task") or t.get("title") or "").strip()
            if text:
                items.append({"text": text, "status": status})
        self.ctx.todos = items
        done = sum(1 for t in items if t["status"] == "done")
        return f"할 일 {len(items)}개 기록(완료 {done})."
