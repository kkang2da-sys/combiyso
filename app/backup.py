# -*- coding: utf-8 -*-
"""파일 백업 / 되돌리기(undo).

파일을 수정·삭제하기 전에 원본을 복사해 두고, UI 에서 되돌릴 수 있게 한다.
"""
import json
import shutil
import time
import uuid
from pathlib import Path

from .config import BACKUPS_DIR

FILES_DIR = BACKUPS_DIR / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = BACKUPS_DIR / "index.json"


def _load() -> list:
    try:
        if INDEX_PATH.exists():
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save(items: list):
    try:
        INDEX_PATH.write_text(json.dumps(items[-2000:], ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:
        pass


def backup(path, reason: str = "edit") -> str:
    """원본을 백업하고 백업 id 반환. 실패하면 빈 문자열."""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        bid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        dst = FILES_DIR / f"{bid}.bak"
        shutil.copy2(str(p), str(dst))
        items = _load()
        items.append({"id": bid, "path": str(p), "name": p.name, "ts": time.time(),
                      "size": dst.stat().st_size, "reason": reason})
        _save(items)
        return bid
    except Exception:
        return ""


def list_backups(limit: int = 300) -> list:
    items = _load()
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items[:limit]


def restore(backup_id: str):
    items = _load()
    rec = next((x for x in items if x.get("id") == backup_id), None)
    if not rec:
        return False, "백업을 찾을 수 없습니다."
    src = FILES_DIR / f"{backup_id}.bak"
    if not src.exists():
        return False, "백업 파일이 없습니다."
    try:
        target = Path(rec["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(target))
        return True, f"되돌렸습니다: {target}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def delete_backup(backup_id: str) -> bool:
    items = _load()
    keep = [x for x in items if x.get("id") != backup_id]
    if len(keep) == len(items):
        return False
    _save(keep)
    try:
        (FILES_DIR / f"{backup_id}.bak").unlink(missing_ok=True)
    except Exception:
        pass
    return True


def clear_all() -> int:
    items = _load()
    n = 0
    for rec in items:
        try:
            (FILES_DIR / f"{rec['id']}.bak").unlink(missing_ok=True)
            n += 1
        except Exception:
            pass
    _save([])
    return n


def prune(keep: int = 300) -> int:
    items = _load()
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    old, items = items[keep:], items[:keep]
    for rec in old:
        try:
            (FILES_DIR / f"{rec['id']}.bak").unlink(missing_ok=True)
        except Exception:
            pass
    if old:
        _save(items)
    return len(old)
