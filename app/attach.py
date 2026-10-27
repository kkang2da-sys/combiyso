# -*- coding: utf-8 -*-
"""첨부(이미지/PDF) 저장·불러오기.

세션 JSON 에 base64 를 넣으면 파일이 거대해지므로, 실제 바이너리는
`%APPDATA%\\compbiyso\\attachments\\<세션id>\\` 에 저장하고 메시지에는 참조만 둔다.
"""
import base64
import re
import time
import uuid
from pathlib import Path

from .config import ATTACH_DIR

_SAFE = re.compile(r"[^A-Za-z0-9_-]")
MAX_IMAGES_PER_MSG = 6


def _sid_dir(sid) -> Path:
    d = ATTACH_DIR / (_SAFE.sub("_", str(sid or "x"))[:40] or "x")
    d.mkdir(parents=True, exist_ok=True)
    return d


def decode_payload(raw: str) -> bytes:
    """`data:image/png;base64,....` 또는 순수 base64 문자열 → bytes."""
    s = str(raw or "").strip()
    if s.startswith("data:"):
        i = s.find(",")
        s = s[i + 1:] if i >= 0 else ""
    s = re.sub(r"\s+", "", s)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return b""


def save_bytes(sid, name: str, data: bytes, mime: str = "", kind: str = "image") -> dict:
    d = _sid_dir(sid)
    ext = Path(str(name or "")).suffix[:10]
    if not ext or len(ext) < 2:
        ext = {"image": ".png", "pdf": ".pdf"}.get(kind, ".bin")
    fn = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ext
    try:
        (d / fn).write_bytes(data)
    except Exception:
        return {}
    return {"id": fn, "name": str(name or fn)[:120], "kind": kind,
            "mime": mime or "", "size": len(data), "ts": time.time()}


def path_of(sid, aid):
    aid = str(aid or "")
    if not aid or "/" in aid or "\\" in aid or aid.startswith("."):
        return None
    p = (_sid_dir(sid) / aid)
    try:
        if p.is_file():
            return p
    except Exception:
        pass
    return None


def read_bytes(sid, aid) -> bytes:
    p = path_of(sid, aid)
    try:
        return p.read_bytes() if p else b""
    except Exception:
        return b""


def b64(sid, aid) -> str:
    data = read_bytes(sid, aid)
    return base64.b64encode(data).decode("ascii") if data else ""


def load_images(sid, refs, max_n: int = MAX_IMAGES_PER_MSG) -> list:
    """메시지의 첨부 참조 → Ollama `images` 용 base64 목록."""
    out = []
    for r in (refs or [])[:max_n]:
        if str(r.get("kind") or "") != "image":
            continue
        b = b64(sid, r.get("id"))
        if b:
            out.append(b)
    return out


def image_mimes(sid, refs, max_n: int = MAX_IMAGES_PER_MSG) -> list:
    out = []
    for r in (refs or [])[:max_n]:
        if str(r.get("kind") or "") == "image":
            out.append(str(r.get("mime") or "image/png"))
    return out


def drop_all(sid) -> bool:
    import shutil
    try:
        d = ATTACH_DIR / (_SAFE.sub("_", str(sid or "x"))[:40] or "x")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        return True
    except Exception:
        return False
