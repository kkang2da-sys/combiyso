# -*- coding: utf-8 -*-
"""설정/데이터 경로 관리 + 자동 성능 프로필 (v1.5.0).

- `effective(cfg)` : 하드웨어에 맞춰 자원 관련 값을 자동으로 낮춘 사본을 돌려준다.
  (사용자가 기본값에서 바꾼 값은 건드리지 않는다 — `perf.adapt`)
- `sanitize(cfg)` : 잘못된 타입/범위의 설정이 들어와도 프로그램이 죽지 않게 보정한다.
"""
import json
import os
import sys
from pathlib import Path

from . import perf

APP_NAME = "컴비서"
APP_EN = "Combiyso"
VERSION = "1.6.5"
DATA_DIRNAME = "compbiyso"          # 데이터 폴더는 ASCII (인코딩 문제 방지)


def data_dir() -> Path:
    if os.name == "nt":
        # 윈도우: 기존과 동일 (설정 안 바뀜)
        base = os.environ.get("APPDATA") or str(Path.home())
    elif sys.platform == "darwin":
        # macOS: ~/Library/Application Support (COMPIYSO_DATA 로 강제 지정 가능)
        base = (os.environ.get("COMPIYSO_DATA")
                or str(Path.home() / "Library" / "Application Support"))
    else:
        # 리눅스: XDG 규칙. COMPIYSO_DATA 로 강제 지정 가능.
        base = (os.environ.get("COMPIYSO_DATA")
                or os.environ.get("XDG_DATA_HOME")
                or str(Path.home() / ".local" / "share"))
    d = Path(base) / DATA_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


_DATA = data_dir()
CONFIG_PATH = _DATA / "config.json"
SESSIONS_DIR = _DATA / "sessions"
BACKUPS_DIR = _DATA / "backups"
LOGS_DIR = _DATA / "logs"
SKILLS_DIR = _DATA / "skills"
COMMANDS_DIR = _DATA / "commands"
AGENTS_DIR = _DATA / "agents"
ATTACH_DIR = _DATA / "attachments"
for _d in (SESSIONS_DIR, BACKUPS_DIR, LOGS_DIR, SKILLS_DIR, COMMANDS_DIR, AGENTS_DIR, ATTACH_DIR):
    _d.mkdir(parents=True, exist_ok=True)


DEFAULT_PERMISSIONS = {
    "read": "allow",       # 읽기/검색/목록
    "write": "ask",        # 파일 쓰기/수정
    "delete": "ask",       # 삭제
    "move": "ask",         # 이동/이름변경
    "copy": "allow",       # 복사
    "mkdir": "allow",      # 폴더 생성
    "shell": "ask",        # 셸 명령 실행
    "network": "allow",    # 웹 검색/페이지 가져오기
    "mcp": "ask",          # MCP 서버 도구
}


DEFAULTS = {
    # ---- 모델 연결 ----
    "provider": "ollama",                        # ollama | openai
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "gemma4:12b",
    "ollama_servers": [],                        # [{name,url}] 다른 PC 포함
    "ollama_api_key": "",                        # 사내 프록시용(선택)
    "ollama_connect_timeout": 8,                 # 연결 타임아웃(초)
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    # v1.6.0: API 키 서비스 여러 개를 저장해 두고 골라 쓴다
    "openai_preset": "",                         # 사용 중인 서비스 id(gemini, groq …)
    "openai_presets": [],                        # [{id,name,base_url,api_key,model}]
    "auto_start_ollama": True,                   # 이 PC Ollama 가 꺼져 있으면 자동 시작
    "lan_scan_subnets": 3,                       # 내부 네트워크 검색 시 훑을 /24 대역 수
    "temperature": 0.3,

    # ---- 작업 공간 / 안전 ----
    "workspace": str(Path.home() / "Documents"),
    "allow_outside_workspace": False,
    "command_timeout": 120,
    "auto_backup": True,
    "backup_keep": 300,

    # ---- 권한 (세밀 규칙) ----
    "permission_actions": dict(DEFAULT_PERMISSIONS),
    "permission_rules": [],                      # [{tool, pattern, action}]

    # ---- 에이전트 / 스킬 / 명령 ----
    "agent": "build",                            # 현재 사용할 에이전트 id
    "skills_enabled": True,
    "disabled_skills": [],
    "commands_enabled": True,
    "subagent_type": "general",                  # task 도구 기본 하위 에이전트

    # ---- 에이전트 동작 ----
    "max_steps": 40,
    "tool_mode": "auto",                         # auto | native | text
    "subagent_max_steps": 20,
    "max_tool_output": 20000,
    "system_extra": "",
    "rules_file": "COMPIYSO.md",
    "use_rules_file": True,

    # ---- 컨텍스트 ----
    "auto_compact": True,
    "compact_threshold": 24000,
    "keep_recent_messages": 8,

    # ---- 하위 에이전트 (병렬) ----
    "subagent_parallel": "auto",                 # auto | on | off
    "subagent_parallel_max": 3,                  # 동시 실행 최대 개수

    # ---- 되돌리기 (git 체크포인트) ----
    "git_undo": "auto",                          # auto | on | off
    "git_undo_max_files": 20000,                 # 이보다 많으면 첫 스냅샷 생략
    "git_undo_max_mb": 400,

    # ---- 첨부 (이미지/PDF) ----
    "attachments_enabled": True,
    "image_max_side": 1568,                      # UI 가 이 픽셀 이하로 줄여서 전송
    "attach_max_upload_mb": 20,
    "image_history_turns": 1,                    # 몇 개 최근 사용자 메시지의 이미지를 모델에 보낼지
    "pdf_max_pages": 50,
    "pdf_max_chars": 60000,

    # ---- 성능 / 자원 (v1.5.0) ----
    "auto_optimize": True,                       # 하드웨어에 맞춰 자동 최적화
    "low_spec": "auto",                          # auto | on | off
    "memory_guard": True,                        # 실행 중 메모리 압박 시 자동 감축
    "release_on_exit": "auto",                   # auto(현재 서버) | on(원격 포함) | off
    "cleanup_on_exit": True,                     # 종료 시 gc + 작업 세트 반납
    "ui_lite": "auto",                           # auto(저사양이면 간소 UI) | on | off
    "ollama_num_ctx": 8192,                      # 0 = Ollama 기본값
    "ollama_num_predict": 0,                     # 0 = 제한 없음
    "ollama_keep_alive": "30m",                  # 모델을 메모리에 유지
    "ollama_think": "auto",                      # auto(모델 기본) | on | off(생각 단계 끄기=빠름)

    # ---- 웹 ----
    "web_max_results": 5,
    "web_timeout": 20,

    # ---- MCP ----
    "mcp_servers": [],                           # [{name,command,args,env,enabled}]

    # ---- UI / 서버 ----
    "theme": "dark",                             # dark | light
    "show_usage": True,
    "notify_sound": False,
    "host": "127.0.0.1",
    "port": 8799,
}

# 저사양 프로필(하위 호환): v1.4.0 의 LOW_SPEC 과 같은 값
LOW_SPEC = dict(perf.PROFILES["low"])

INT_BOUNDS = {
    "command_timeout": (5, 3600),
    "backup_keep": (10, 5000),
    "max_steps": (1, 200),
    "subagent_max_steps": (1, 100),
    "max_tool_output": (1000, 200000),
    "compact_threshold": (1000, 200000),
    "keep_recent_messages": (2, 100),
    "subagent_parallel_max": (2, 8),
    "git_undo_max_files": (100, 1000000),
    "git_undo_max_mb": (10, 102400),
    "image_max_side": (256, 4096),
    "attach_max_upload_mb": (1, 128),
    "image_history_turns": (1, 10),
    "pdf_max_pages": (1, 1000),
    "pdf_max_chars": (1000, 500000),
    "ollama_num_ctx": (0, 131072),
    "ollama_num_predict": (0, 32768),
    "ollama_connect_timeout": (2, 120),
    "lan_scan_subnets": (1, 8),
    "web_max_results": (1, 20),
    "web_timeout": (3, 300),
    "port": (1, 65535),
}
FLOAT_BOUNDS = {"temperature": (0.0, 2.0)}
ENUMS = {
    "provider": ("ollama", "openai"),
    "tool_mode": ("auto", "native", "text"),
    "low_spec": ("auto", "on", "off"),
    "subagent_parallel": ("auto", "on", "off"),
    "git_undo": ("auto", "on", "off"),
    "release_on_exit": ("auto", "on", "off"),
    "ui_lite": ("auto", "on", "off"),
    "theme": ("dark", "light"),
    "ollama_think": ("auto", "on", "off"),
}
BOOL_KEYS = ("auto_optimize", "memory_guard", "cleanup_on_exit", "auto_backup",
             "allow_outside_workspace", "auto_compact", "skills_enabled",
             "commands_enabled", "attachments_enabled", "use_rules_file",
             "show_usage", "notify_sound", "auto_start_ollama")


def mem_info():
    """(전체 MB, 사용가능 MB). 실패하면 (0, 0)."""
    return perf.mem_info()


def is_low_spec(cfg=None):
    try:
        return perf.resolve_tier(cfg or {}) in ("lite", "low")
    except Exception:
        return False


def effective(cfg: dict) -> dict:
    """저사양/자동 최적화가 적용된 설정 **사본**.

    반환값의 `_low_spec_active`, `_perf_tier`, `_perf_applied` 로 적용 여부를 알 수 있다.
    """
    try:
        return perf.adapt(cfg or {}, DEFAULTS)
    except Exception:
        out = dict(cfg or {})
        out.setdefault("_low_spec_active", False)
        out.setdefault("_perf_tier", "balanced")
        out.setdefault("_perf_applied", {})
        return out


# 이전 버전 설정 마이그레이션: 예전 on/off 토글 → 권한 표
_LEGACY_TOGGLES = {
    "allow_write": ("write", "delete", "move"),
    "allow_delete": ("delete",),
    "allow_shell": ("shell",),
    "allow_network": ("network",),
}


def _migrate(data: dict, merged: dict):
    changed = False
    actions = dict(merged.get("permission_actions") or {})
    for key, kinds in _LEGACY_TOGGLES.items():
        if key in data and isinstance(data[key], bool):
            changed = True
            if not data[key]:
                for k in kinds:
                    actions[k] = "deny"
    if "approval_mode" in data:
        changed = True
        mode = data.pop("approval_mode")
        if mode == "auto":
            for k, v in actions.items():
                if v == "ask":
                    actions[k] = "allow"
    if changed:
        merged["permission_actions"] = actions

    # v1.4.0 이하: low_spec 이 bool 이던 시절
    if isinstance(merged.get("low_spec"), bool):
        merged["low_spec"] = "on" if merged["low_spec"] else "off"

    # v1.5.0: 다른 PC 모델 서버 목록 정규화("url 문자열" 목록도 허용)
    servers = merged.get("ollama_servers")
    if isinstance(servers, list):
        clean = []
        for i, s in enumerate(servers):
            if isinstance(s, str) and s.strip():
                clean.append({"id": f"srv{i + 1}", "name": s.strip(),
                              "url": perf.normalize_url(s)})
            elif isinstance(s, dict) and (s.get("url") or "").strip():
                clean.append({"id": str(s.get("id") or f"srv{i + 1}"),
                              "name": str(s.get("name") or "").strip(),
                              "url": perf.normalize_url(s.get("url"))})
        merged["ollama_servers"] = clean

    # 주소에 http:// 나 포트가 없어도 동작하게
    merged["ollama_url"] = perf.normalize_url(merged.get("ollama_url"))
    return merged


def _as_bool(v):
    """'false'/'0'/'no'/'off' 문자열도 False 로 본다(옛 설정·수동 편집 대비)."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(v)


def sanitize(cfg: dict) -> dict:
    """잘못된 타입/범위를 보정한다. 알 수 없는 키는 그대로 둔다."""
    out = dict(cfg or {})
    for k in BOOL_KEYS:
        if k in out:
            out[k] = _as_bool(out[k])
    for k, allowed in ENUMS.items():
        if k in out:
            if isinstance(out[k], str):
                out[k] = out[k].strip().lower()
            if out[k] not in allowed:
                out[k] = DEFAULTS.get(k)
    for k, (lo, hi) in INT_BOUNDS.items():
        if k not in out:
            continue
        try:
            v = int(out[k])
        except Exception:
            out[k] = DEFAULTS.get(k)
            continue
        out[k] = max(lo, min(hi, v))
    for k, (lo, hi) in FLOAT_BOUNDS.items():
        try:
            v = float(out.get(k, DEFAULTS.get(k)))
        except Exception:
            v = float(DEFAULTS.get(k) or 0)
        out[k] = max(lo, min(hi, v))
    for k in ("permission_rules", "mcp_servers", "ollama_servers",
              "openai_presets", "disabled_skills"):
        if k in out and not isinstance(out[k], list):
            out[k] = []
    out["ollama_url"] = perf.normalize_url(out.get("ollama_url"))
    if not isinstance(out.get("permission_actions"), dict):
        out["permission_actions"] = dict(DEFAULT_PERMISSIONS)
    if not isinstance(out.get("workspace"), str) or not out.get("workspace").strip():
        out["workspace"] = DEFAULTS["workspace"]
    for k in ("agent", "subagent_type", "rules_file", "ollama_model"):
        if k in out and not isinstance(out[k], str):
            out[k] = str(out[k] or "")
    if "ollama_servers" in out:
        fixed = []
        for i, s in enumerate(out["ollama_servers"]):
            if isinstance(s, str):
                s = {"name": s, "url": s}
            if not isinstance(s, dict):
                continue
            raw_url = str(s.get("url") or "").strip()
            if not raw_url:
                continue
            url = perf.normalize_url(raw_url)
            if not url:
                continue
            fixed.append({"id": str(s.get("id") or f"srv{i + 1}"),
                          "name": str(s.get("name") or "").strip()[:60],
                          "url": url})
        out["ollama_servers"] = fixed
    # v1.6.0: API 키 서비스 목록 정규화
    if "openai_presets" in out:
        fixed_api = []
        for i, p in enumerate(out["openai_presets"]):
            if not isinstance(p, dict):
                continue
            base = str(p.get("base_url") or "").strip()
            if not base:
                continue
            fixed_api.append({
                "id": str(p.get("id") or f"api{i + 1}"),
                "name": str(p.get("name") or base).strip()[:60],
                "base_url": base.rstrip("/"),
                "api_key": str(p.get("api_key") or ""),
                "model": str(p.get("model") or "").strip()[:120],
            })
        out["openai_presets"] = fixed_api
    if not isinstance(out.get("openai_preset"), str):
        out["openai_preset"] = ""
    return out


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["permission_actions"] = dict(DEFAULT_PERMISSIONS)
    cfg["ollama_servers"] = []
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
                cfg = _migrate(dict(data), cfg)
    except Exception:
        pass
    cfg = sanitize(cfg)
    if not isinstance(cfg.get("permission_actions"), dict):
        cfg["permission_actions"] = dict(DEFAULT_PERMISSIONS)
    return cfg


def save_config(cfg: dict) -> dict:
    merged = dict(DEFAULTS)
    merged["permission_actions"] = dict(DEFAULT_PERMISSIONS)
    incoming = {k: v for k, v in (cfg or {}).items() if not str(k).startswith("_")}
    merged.update(incoming)
    merged = sanitize(merged)
    try:
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass
    return merged


def reset_config() -> dict:
    return save_config({})
