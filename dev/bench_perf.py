# -*- coding: utf-8 -*-
"""성능 기준선 측정 (세션 목록/저장/토큰 추정).

실행:  .venv\\Scripts\\python.exe dev\\bench_perf.py
실제 사용자 데이터는 건드리지 않고 임시 폴더를 씁니다.
"""
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import context, sessions

N_SESSIONS = 200
N_MESSAGES = 40
MSG = ("가나다라마바사아자차" * 20) + ("hello world " * 40)   # 약 1.4KB


def bench(fn, repeat=5):
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(times), 1)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="combiyso_bench_"))
    try:
        # 1) 세션 200개 생성 (저장 포맷은 현재 구현과 동일하게)
        t0 = time.perf_counter()
        for i in range(N_SESSIONS):
            msgs = [{"role": "user" if j % 2 == 0 else "assistant",
                     "content": f"[{i}-{j}] " + MSG, "ts": time.time()}
                    for j in range(N_MESSAGES)]
            s = {"id": f"s{i:04d}", "title": f"세션 {i}", "created": time.time(),
                 "updated": time.time() - i, "messages": msgs, "todos": []}
            (tmp / f"{s['id']}.json").write_text(
                json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        seed = time.perf_counter() - t0

        sessions.SESSIONS_DIR = tmp

        # 2) 목록 (UI가 자주 호출)
        list_ms = bench(lambda: sessions.list_sessions())

        # 3) 검색
        search_ms = bench(lambda: sessions.search("hello"), repeat=3)

        # 4) 저장 (긴 세션 1개)
        s = json.loads((tmp / "s0000.json").read_text(encoding="utf-8"))
        save_ms = bench(lambda: sessions.save(s), repeat=10)

        # 5) 토큰 추정 (긴 텍스트)
        big = MSG * 60
        est_ms = bench(lambda: context.estimate_tokens(big), repeat=20)
        msgs = json.loads((tmp / "s0000.json").read_text(encoding="utf-8"))["messages"]
        mt_ms = bench(lambda: context.messages_tokens(msgs), repeat=10)

        total_kb = sum(p.stat().st_size for p in tmp.glob("*.json")) / 1024
        print(json.dumps({
            "sessions": N_SESSIONS,
            "messages_per_session": N_MESSAGES,
            "total_data_kb": round(total_kb),
            "seed_ms": round(seed * 1000),
            "list_sessions_ms": list_ms,
            "search_ms": search_ms,
            "save_ms": save_ms,
            "estimate_tokens_ms": est_ms,
            "messages_tokens_ms": mt_ms,
        }, ensure_ascii=False, indent=1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
