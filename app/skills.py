# -*- coding: utf-8 -*-
"""스킬(SKILL.md) 관리.

opencode 와 같은 방식(점진적 공개 / progressive disclosure):
- 시스템 프롬프트에는 스킬의 **이름 + 설명만** 넣는다(토큰 절약)
- 에이전트가 필요할 때 `skill` 도구로 본문 전체와 리소스 목록을 불러온다

폴더 구조
  %APPDATA%\\compbiyso\\skills\\
    <스킬id>\\
      SKILL.md          # --- frontmatter(name, description) --- + 본문
      (그 밖의 참고 파일 = 리소스)
"""
import re
import shutil
import time
from pathlib import Path

from .config import SKILLS_DIR

# 경로 구분자/윈도우 금지문자만 막는다(한글 id 허용)
ID_RE = re.compile(r"^[^\s/\\:*?\"<>|]{1,64}$")
_FM_RE = re.compile(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)$", re.S)
MAX_BODY = 120_000


def valid_id(sid: str) -> bool:
    return bool(sid) and bool(ID_RE.match(str(sid).strip())) and str(sid).strip() not in (".", "..")


def skill_dir(sid: str) -> Path:
    return SKILLS_DIR / str(sid).strip()


def parse_frontmatter(text: str):
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


def dump_frontmatter(name: str, description: str, body: str) -> str:
    lines = ["---", f"name: {name}", f"description: {description}", "---", ""]
    return "\n".join(lines) + (body or "").strip() + "\n"


def resource_files(sid: str) -> list:
    d = skill_dir(sid)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.name.lower() != "skill.md":
            try:
                rel = p.relative_to(d)
            except Exception:
                rel = p.name
            out.append({"path": str(rel).replace("\\", "/"), "size": p.stat().st_size})
    return out[:200]


def get_skill(sid: str, with_body: bool = True):
    d = skill_dir(sid)
    if not d.is_dir():
        return None
    md = None
    for cand in ("SKILL.md", "skill.md", "Skill.md"):
        p = d / cand
        if p.is_file():
            md = p
            break
    if not md:
        return None
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    meta, body = parse_frontmatter(raw)
    info = {
        "id": str(sid),
        "name": meta.get("name") or str(sid),
        "description": meta.get("description") or "",
        "body": body.strip() if with_body else "",
        "path": str(md),
        "dir": str(d),
        "resources": resource_files(sid),
        "updated": md.stat().st_mtime,
    }
    return info


def list_skills():
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        s = get_skill(d.name, with_body=False)
        if s:
            out.append(s)
    return out


def save_skill(sid: str, name: str = "", description: str = "", body: str = ""):
    sid = str(sid).strip()
    if not valid_id(sid):
        raise ValueError("스킬 id 는 공백/특수문자(\\ / : * ? \" < > |) 없이 64자 이하로 정하세요.")
    d = skill_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    text = dump_frontmatter((name or sid).strip(), (description or "").strip(),
                            str(body or "")[:MAX_BODY])
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return get_skill(sid, with_body=False)


def delete_skill(sid: str) -> bool:
    if not valid_id(sid):
        return False
    d = skill_dir(sid)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


def read_resource(sid: str, rel: str, max_chars: int = 40000) -> str:
    """스킬 폴더 안의 리소스 파일 읽기(경로 탈출 방지)."""
    d = skill_dir(sid).resolve()
    p = (d / str(rel)).resolve()
    try:
        p.relative_to(d)
    except Exception:
        return "허용되지 않은 경로입니다."
    if not p.is_file():
        return "파일이 없습니다."
    try:
        raw = p.read_bytes()[:max_chars * 4]
        if b"\x00" in raw[:2000]:
            return "바이너리 파일입니다."
        return raw.decode("utf-8", errors="replace")[:max_chars]
    except Exception as e:
        return f"읽기 실패: {type(e).__name__}: {e}"


def ensure_examples():
    """처음 실행 시 참고용 스킬 1개 생성(이미 스킬이 있으면 아무것도 안 함)."""
    if list_skills():
        return False
    try:
        save_skill(
            "example-report",
            name="보고서 만들기",
            description=("작업 폴더를 조사해 한국어 Markdown 보고서를 만들 때 사용. "
                         "파일 목록·핵심 내용·할 일을 정리한 report.md 를 생성한다."),
            body=(
                "## 보고서 작성 절차\n"
                "1. `list_dir`(recursive=true) 로 폴더 구조를 파악한다.\n"
                "2. 중요한 파일 3~7개를 `read_file` 로 읽는다.\n"
                "3. 아래 형식으로 `report.md` 를 `write_file` 로 저장한다.\n\n"
                "```markdown\n"
                "# <폴더 이름> 보고서\n\n"
                "## 1. 개요\n- 목적: ...\n\n"
                "## 2. 파일 구성\n| 경로 | 역할 |\n|---|---|\n\n"
                "## 3. 주요 발견\n- ...\n\n"
                "## 4. 다음 할 일\n- [ ] ...\n"
                "```\n\n"
                "주의: 추측하지 말고 실제로 읽은 내용만 쓴다. 저장 전에 사용자 승인을 받는다."
            ),
        )
        return True
    except Exception:
        return False
