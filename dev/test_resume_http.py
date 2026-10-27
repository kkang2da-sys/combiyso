# -*- coding: utf-8 -*-
"""중단 복구(resume) 전용 HTTP 테스트. 서버 + Ollama 가 떠 있어야 한다."""
import json
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8799"
PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}   {str(detail)[:200]}")


def main():
    from app import runs as R
    from app import sessions as S

    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            if requests.get(BASE + "/api/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)

    s = S.new_session()
    s["title"] = "중단 복구 테스트"
    s["messages"].append({"role": "user",
                          "content": "자기소개를 아주 짧게 한 줄로 해줘.",
                          "ts": time.time()})
    S.save(s)
    rec = R.start(s["id"], "build", note="테스트")
    R.save_state(rec["id"], messages=[{"role": "user", "content": s["messages"][0]["content"]}],
                 step=1, phase="생각 중")
    R.finish(rec["id"], "interrupted", "테스트 강제 중단")
    print(f"run={rec['id']} session={s['id']}")

    j = requests.get(BASE + "/api/runs", timeout=10).json()
    mine = [r for r in j.get("runs", []) if r["id"] == rec["id"]]
    ok("저널 API 노출", len(mine) == 1, j)
    ok("이어서 가능", bool(mine and mine[0]["resumable"]) and not mine[0]["active"],
       mine[:1])
    ok("여유메모리", isinstance(j.get("free_mb"), int) and j["free_mb"] > 0, j.get("free_mb"))

    sl = requests.get(BASE + "/api/sessions", timeout=10).json()
    it = [x for x in sl if x["id"] == s["id"]]
    ok("세션 목록 재개 표시", bool(it and it[0].get("resumable")), it[:1])

    events = []
    with requests.post(BASE + "/api/resume", json={"run_id": rec["id"]},
                       stream=True, timeout=900) as r:
        ok("resume HTTP 200", r.status_code == 200,
           f"{r.status_code} {r.text[:200]}")
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:].strip()))
    types = [e.get("type") for e in events]
    print("이벤트:", types[:20])
    ok("run 이벤트", "run" in types)
    ok("status 이벤트", "status" in types)
    ok("final 이벤트", "final" in types)
    ok("done 이벤트", "done" in types)
    rtext = ""
    for e in events:
        if e.get("type") == "final":
            rtext = e.get("text") or ""
    ok("답변 생성", len(rtext) > 3, rtext[:100])

    full = requests.get(BASE + "/api/sessions/" + s["id"], timeout=10).json()
    roles = [m["role"] for m in full.get("messages", [])]
    ok("세션에 답변 저장", any(m["role"] == "assistant" and m.get("content")
                        for m in full.get("messages", [])), roles)
    ok("사용자 메시지 중복 없음", roles.count("user") == 1, roles)
    ok("실행 완료 처리", (R.get(rec["id"]) or {}).get("status") == "done")

    d = requests.post(BASE + "/api/runs/discard", json={"run_id": rec["id"]}, timeout=10).json()
    ok("저널 삭제", d.get("ok"))
    requests.delete(BASE + "/api/sessions/" + s["id"], timeout=10)

    print("\n" + "=" * 64)
    print(f"결과:  통과 {PASS}  /  실패 {FAIL}")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
