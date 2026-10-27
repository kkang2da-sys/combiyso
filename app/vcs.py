# -*- coding: utf-8 -*-
"""git 기반 체크포인트 / 되돌리기.

설계 원칙
- **사용자 워크스페이스에 `.git` 을 만들지 않는다.** 데이터 폴더 안에 전용 git
  디렉터리를 두고 `GIT_DIR`/`GIT_WORK_TREE` 로만 조작한다(사용자의 실제 git 저장소를
  건드리지 않음).
- 스냅샷은 "도구가 파일을 바꾸기 직전"에 1회 자동 생성된다(에이전트 실행당 1개).
- 되돌리기는 `git checkout <sha> -- .` 로 **내용만** 복원하고, 그 사이 새로 생긴
  파일(= 스냅샷에 기록된, 에이전트가 만든 파일)만 지운다. `git clean` 은 쓰지 않는다.
  → 추적되지 않는 사용자 파일(무시된 파일 등)은 절대 건드리지 않는다.
- 되돌리기 직전 상태도 스냅샷으로 남겨 **되돌리기의 되돌리기**가 가능하다.
"""
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import data_dir

CKPT_DIR = data_dir() / "checkpoints"
CKPT_DIR.mkdir(parents=True, exist_ok=True)

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea",
             ".mypy_cache", ".pytest_cache", "dist", "build"}
EXCLUDE_LINES = [".git/", ".git", "*.pyc", "__pycache__/", "node_modules/",
                 ".venv/", "venv/", "*.tmp"]


# --------------------------------------------------------------------------- #
def available() -> bool:
    """git 실행 파일이 있는가."""
    return bool(shutil.which("git"))


def enabled(cfg) -> bool:
    mode = str((cfg or {}).get("git_undo") or "auto").lower()
    if mode == "off":
        return False
    return available()


def _git_dir(ws: str) -> Path:
    h = hashlib.sha1(str(Path(ws).resolve()).encode("utf-8")).hexdigest()[:16]
    return CKPT_DIR / f"{h}.git"


def _run(args, git_dir: Path, ws: str, timeout: int = 180, check: bool = False):
    env = dict(os.environ)
    env.update({
        "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(ws),
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "컴비서", "GIT_AUTHOR_EMAIL": "combiyso@local",
        "GIT_COMMITTER_NAME": "컴비서", "GIT_COMMITTER_EMAIL": "combiyso@local",
        "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
    })
    cmd = ["git", "-c", "core.hooksPath=", "-c", "i18n.commitEncoding=utf-8",
           "-c", "i18n.logOutputEncoding=utf-8"] + list(args)
    try:
        p = subprocess.run(cmd, cwd=str(ws), env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        if check:
            raise RuntimeError(f"{type(e).__name__}: {e}")
        return 1, "", f"{type(e).__name__}: {e}"
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if check and p.returncode != 0:
        raise RuntimeError(err or f"git {args[0]} 실패({p.returncode})")
    return p.returncode, out, err


def _ensure(git_dir: Path, ws: str):
    if (git_dir / "HEAD").exists():
        return
    git_dir.mkdir(parents=True, exist_ok=True)
    _run(["init", "--quiet"], git_dir, ws, timeout=60)
    for k, v in (("core.autocrlf", "false"), ("core.fileMode", "false"),
                 ("core.quotepath", "false"), ("core.safecrlf", "false"),
                 ("gc.auto", "0")):
        _run(["config", k, v], git_dir, ws, timeout=30)
    try:
        ex = git_dir / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text("\n".join(EXCLUDE_LINES) + "\n", encoding="utf-8")
    except Exception:
        pass


def _too_big(ws: str, max_files: int, max_mb: int):
    """워크스페이스가 너무 크면 (True, 파일수, MB) — 조기 종료."""
    files, size, limit = 0, 0, max(1, max_mb) * 1024 * 1024
    try:
        for root, dirs, names in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                files += 1
                try:
                    size += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
                if files > max_files or size > limit:
                    return True, files, size // 1048576
    except Exception:
        return False, files, size // 1048576
    return False, files, size // 1048576


# --------------------------------------------------------------------------- #
def workspace_of(cfg) -> str:
    return str(Path(str((cfg or {}).get("workspace") or "")).expanduser())


def status(cfg) -> dict:
    ws = workspace_of(cfg)
    info = {"available": available(), "enabled": enabled(cfg), "workspace": ws,
            "initialized": False, "count": 0, "last": None}
    if not info["available"] or not ws or not Path(ws).is_dir():
        return info
    gd = _git_dir(ws)
    if not (gd / "HEAD").exists():
        return info
    info["initialized"] = True
    rows = checkpoints(cfg, limit=1)
    if rows:
        info["last"] = rows[0]
        rc, out, _ = _run(["rev-list", "--count", "HEAD"], gd, ws, timeout=30)
        try:
            info["count"] = int(out)
        except Exception:
            info["count"] = len(rows)
    return info


def snapshot(cfg, label: str = "checkpoint", force: bool = False):
    """현재 상태를 커밋. 반환 dict({ok, sha, unchanged, reason}) 또는 None(기능 꺼짐)."""
    if not enabled(cfg):
        return None
    ws = workspace_of(cfg)
    if not ws or not Path(ws).is_dir():
        return None
    gd = _git_dir(ws)
    first = not (gd / "HEAD").exists()
    if first and not force:
        big, files, mb = _too_big(ws, int(cfg.get("git_undo_max_files") or 20000),
                                  int(cfg.get("git_undo_max_mb") or 400))
        if big:
            return {"ok": False,
                    "reason": f"워크스페이스가 너무 큽니다(약 {files}개 파일 / {mb}MB). "
                              f"설정→되돌리기 에서 범위를 늘리거나 끄세요."}
    _ensure(gd, ws)
    rc, out, err = _run(["add", "-A", "--ignore-errors"], gd, ws, timeout=300)
    if rc != 0:
        return {"ok": False, "reason": (err or "git add 실패")[:300]}
    rc, out, err = _run(["status", "--porcelain"], gd, ws, timeout=60)
    if rc == 0 and not out:
        rc2, sha, _ = _run(["rev-parse", "HEAD"], gd, ws, timeout=30)
        return {"ok": True, "sha": sha, "unchanged": True}
    rc, out, err = _run(["commit", "--allow-empty", "--no-verify",
                         "-m", str(label or "checkpoint")[:200]],
                        gd, ws, timeout=180)
    if rc != 0:
        return {"ok": False, "reason": (err or "git commit 실패")[:300]}
    rc, sha, _ = _run(["rev-parse", "HEAD"], gd, ws, timeout=30)
    return {"ok": True, "sha": sha, "ts": time.time(), "label": label}


def checkpoints(cfg, limit: int = 40) -> list:
    ws = workspace_of(cfg)
    gd = _git_dir(ws) if ws else None
    if not gd or not (gd / "HEAD").exists():
        return []
    rc, out, _ = _run(["log", f"-n{max(1, limit)}", "--pretty=format:%H%x1f%ct%x1f%s"],
                      gd, ws, timeout=30)
    rows = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 3:
                rows.append({"sha": parts[0], "short": parts[0][:8],
                             "ts": int(parts[1] or 0), "label": parts[2]})
    return rows


def _added_between(gd: Path, ws: str, sha: str, ref: str = "HEAD") -> list:
    rc, out, _ = _run(["diff", "--name-only", "--diff-filter=A", sha, ref],
                      gd, ws, timeout=120)
    return [f.strip() for f in out.splitlines() if f.strip()] if rc == 0 else []


def restore(cfg, sha: str):
    """(ok, 메시지) — 되돌리기 전 상태도 스냅샷으로 남긴다."""
    ws = workspace_of(cfg)
    gd = _git_dir(ws) if ws else None
    if not gd or not (gd / "HEAD").exists():
        return False, "체크포인트가 없습니다."
    rc, _, _ = _run(["cat-file", "-e", f"{sha}^{{commit}}"], gd, ws, timeout=30)
    if rc != 0:
        return False, "해당 체크포인트를 찾을 수 없습니다."

    snapshot(cfg, label="before-undo", force=True)
    added = _added_between(gd, ws, sha)
    rc, _, err = _run(["checkout", sha, "--", "."], gd, ws, timeout=300)
    if rc != 0:
        return False, (err or "checkout 실패")[:300]

    removed = 0
    for f in added:
        p = Path(ws) / f
        try:
            if p.is_file():
                p.unlink()
                removed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                removed += 1
        except Exception:
            pass
    snapshot(cfg, label=f"undo->{sha[:8]}", force=True)

    rc, restored, _ = _run(["diff", "--name-only", sha], gd, ws, timeout=60)
    n_restored = len([x for x in restored.splitlines() if x.strip()])
    msg = f"{sha[:8]} 시점으로 되돌렸습니다."
    if removed:
        msg += f" (그 뒤 생긴 파일 {removed}개 삭제)"
    if n_restored:
        msg += f" 확인 필요 {n_restored}개."
    return True, msg


def make_checkpoint(cfg, label: str = ""):
    return snapshot(cfg, label=label or f"수동 {time.strftime('%m-%d %H:%M:%S')}",
                    force=True)


def clear(cfg) -> bool:
    ws = workspace_of(cfg)
    if not ws:
        return False
    gd = _git_dir(ws)
    try:
        if gd.exists():
            shutil.rmtree(gd, ignore_errors=True)
        return True
    except Exception:
        return False
