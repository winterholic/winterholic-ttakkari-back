"""G29b 수동 측정 도구: 오브젝트 선택 → 실제 worker 요청 → 바뀐 소스 줄이 정답인지 센다. pytest 가 수집하지 않는다(이름이 test_ 로 시작하지 않음).

격리 따까리(API·게임 origin 이 떠 있고 TTAKKARI_* 가 격리 값)에서 실제 Claude worker 를 부른다. 운영 DB·.env 는 쓰지 않는다.
사용: TTAKKARI_NO_ENV_FILE=1 (격리 env) .venv/bin/python tests/game_smoke/pick_bench.py <결과 폴더> [easy|hard] [대상 키 ...]
easy 는 소스에 화면 비율 좌표가 그대로 있고 머리 주석이 있는 쉬운 게임, hard 는 격자 함수로 계산한 픽셀 좌표(화면 비율과 바로 대응하지 않음)에 주석이 없는 게임이다.
각 대상마다 저장소를 기준 커밋으로 되돌리고(격리 저장소) 같은 문장("이 오브젝트를 20% 크게 해줘")과 실제 bridge pick 응답을 보낸다.
결과 분류: HIT(정답 줄만 바뀜), PARTIAL(정답 줄과 다른 줄이 함께), WRONG(다른 줄만), ASK(소스 변경 없이 보고·질문), OTHER(오브젝트 줄이 아닌 곳만).
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

from ttakkari.security.auth import issue_access_token

API = "http://127.0.0.1:18790"
HOST_PORT = 18796
HERE = Path(__file__).parent
BACK = HERE.parent.parent
FIXTURE = HERE / "pick_bench_game.ts"
FIXTURE_HARD = HERE / "pick_bench_hard_game.ts"
VIEW_W, VIEW_H = 800, 900  # pick_bench_collect.mjs 의 뷰포트. 캔버스가 이 크기라 픽셀 좌표 / 크기 = 화면 비율
INSTRUCTION = "이 오브젝트를 20% 크게 해줘"

# 키: (화면 비율 좌표, 소스 줄을 찾는 좌표 문자열, 조건)
TARGETS: dict[str, tuple[tuple[float, float], str, str]] = {
    "hero": ((0.2, 0.15), "width * 0.2, height * 0.15", "이름 있음(고유)"),
    "enemy": ((0.8, 0.15), "width * 0.8, height * 0.15", "이름 있음(고유)"),
    "hud-score": ((0.5, 0.05), "width * 0.5, height * 0.05", "이름 있음(텍스트)"),
    "coin": ((0.2, 0.35), "width * 0.2, height * 0.35", "이름 없음·텍스처 키만"),
    "gem": ((0.8, 0.35), "width * 0.8, height * 0.35", "이름 없음·텍스처 키만"),
    "ready-text": ((0.5, 0.5), "width * 0.5, height * 0.5", "이름 없음·텍스트만"),
    "purple-rect": ((0.2, 0.65), "width * 0.2, height * 0.65", "이름·텍스처·텍스트 없음(비슷한 도형 둘)"),
    "orange-rect": ((0.8, 0.65), "width * 0.8, height * 0.65", "이름·텍스처·텍스트 없음(비슷한 도형 둘)"),
    "tree-left": ((0.25, 0.85), "width * 0.25, height * 0.85", "같은 이름 둘(왼쪽)"),
    "tree-right": ((0.75, 0.85), "width * 0.75, height * 0.85", "같은 이름 둘(오른쪽)"),
}


def cell(col: int, row: int) -> tuple[float, float]:
    return (50 + col * 100) / VIEW_W, (80 + row * 120) / VIEW_H


TARGETS_HARD: dict[str, tuple[tuple[float, float], str, str]] = {
    "player": (cell(1, 0), "player.x, player.y", "이름 있음(좌표는 계산값)"),
    "star": (cell(2, 1), "pickup.x, pickup.y", "이름 없음·텍스처 키만(좌표는 계산값)"),
    "level-text": (cell(3, 0), "bannerPos.x, bannerPos.y", "이름 없음·텍스트만"),
    "gate-left": (cell(1, 2), "gateA.x, gateA.y", "이름·텍스처·텍스트 없음, 똑같은 도형 둘(왼쪽)"),
    "gate-right": (cell(4, 2), "gateB.x, gateB.y", "이름·텍스처·텍스트 없음, 똑같은 도형 둘(오른쪽)"),
    "tree-left": (cell(0, 4), "treeA.x, treeA.y", "같은 이름 둘(왼쪽)"),
    "tree-right": (cell(5, 4), "treeB.x, treeB.y", "같은 이름 둘(오른쪽)"),
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=g29b", "-c", "user.email=g29b@local", *args], capture_output=True, text=True, check=True).stdout


def headers() -> dict:
    return {"Authorization": f"Bearer {issue_access_token()[0]}"}


def wait_build(game_id: str, build_id: str) -> dict:
    for _ in range(200):
        b = httpx.get(f"{API}/api/games/{game_id}/builds/{build_id}", headers=headers(), timeout=30).json()
        if b["status"] != "building":
            return b
        time.sleep(3)
    raise SystemExit("빌드가 끝나지 않았다")


def object_lines(text: str, targets: dict) -> dict[str, str | None]:
    lines = text.splitlines()
    return {k: next((ln for ln in lines if needle in ln and "this.add." in ln), None) for k, (_, needle, _) in targets.items()}


def classify(target: str, before: dict, after: dict, other_files: bool, committed: bool) -> str:
    changed = {k for k in before if before[k] is not None and before[k] != after.get(k)}
    if not changed:
        return "OTHER" if other_files else "ASK"
    if changed == {target}:
        return "HIT"
    return "PARTIAL" if target in changed else "WRONG"


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    which = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("easy", "hard") else "easy"
    targets = TARGETS_HARD if which == "hard" else TARGETS
    fixture = FIXTURE_HARD if which == "hard" else FIXTURE
    keys = [k for k in sys.argv[2:] if k in targets] or list(targets)
    slug = "pick-bench-hard" if which == "hard" else "pick-bench"
    r = httpx.post(f"{API}/api/games/create", json={"idea": "소스 매핑 측정", "name": "선택 측정", "slug": slug, "start_development": False, "create_pms": False},
                   headers=headers(), timeout=60)
    if r.status_code == 409:
        game = next(g for g in httpx.get(f"{API}/api/games", headers=headers()).json() if g["slug"] == slug)
    else:
        r.raise_for_status()
        game = r.json()["game"]
    root = Path("/private/tmp/gs-g16/games") / slug
    (root / "src" / "main.ts").write_text(fixture.read_text())
    git(root, "add", "-A")
    if git(root, "status", "--porcelain").strip():
        git(root, "commit", "-q", "-m", "pick bench fixture")
    base = git(root, "rev-parse", "HEAD").strip()
    b = httpx.post(f"{API}/api/games/{game['id']}/builds", json={}, headers=headers(), timeout=60).json()
    build = wait_build(game["id"], b["id"])
    assert build["status"] == "succeeded", build.get("error")

    host_origin = f"http://127.0.0.1:{HOST_PORT}"
    play = httpx.post(f"{API}/api/games/{game['id']}/play", json={"build_id": build["id"]}, headers={**headers(), "Origin": host_origin}, timeout=30).json()
    cfg = {"playwright_dir": str(BACK / ".browser-runtime"), "host_port": HOST_PORT, "play_url": play["url"], "sandbox": play["sandbox"],
           "targets": {k: targets[k][0] for k in keys}}
    proc = subprocess.run(["node", str(HERE / "pick_bench_collect.mjs"), json.dumps(cfg)], capture_output=True, text=True, timeout=240, check=False)
    picks = json.loads(next(ln for ln in proc.stdout.splitlines() if ln.startswith("@@RESULT@@"))[len("@@RESULT@@"):])
    (out / "picks.json").write_text(json.dumps(picks, ensure_ascii=False, indent=1))

    before = object_lines((root / "src" / "main.ts").read_text(), targets)
    results = []
    for key in keys:
        git(root, "reset", "-q", "--hard", base)
        resp = picks[key]
        hit = resp["hits"][0] if resp.get("supported") and resp.get("hits") else None
        row = {"target": key, "condition": targets[key][2], "top_hit": hit and {k: hit[k] for k in ("name", "type", "texture", "text")}, "hit_count": len(resp.get("hits", []))}
        if hit is None:
            row["class"] = "NO_PICK"
            results.append(row)
            continue
        feedback = {"build_id": build["id"], "pick_point": {"x": targets[key][0][0], "y": targets[key][0][1]}, "picked": [hit]}
        req = httpx.post(f"{API}/api/games/{game['id']}/requests", json={"kind": "modify", "instruction": INSTRUCTION, "feedback": feedback, "auto_build": False},
                         headers=headers(), timeout=60).json()
        run = {}
        for _ in range(300):
            run = httpx.get(f"{API}/api/runs/{req['run_id']}", headers=headers(), timeout=30).json()
            if run["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(4)
        after_text = (root / "src" / "main.ts").read_text()
        other_files = bool(git(root, "diff", "--name-only", base, "--", ".", ":(exclude)src/main.ts").strip())
        committed = git(root, "rev-parse", "HEAD").strip() != base
        row |= {"class": classify(key, before, object_lines(after_text, targets), other_files, committed), "run_status": run["status"], "run_id": req["run_id"], "cost_usd": run.get("cost_usd"),
                "committed": committed, "result": (run.get("result_text") or "")[:400]}
        results.append(row)
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(key, row["class"], row.get("cost_usd"), flush=True)
    git(root, "reset", "-q", "--hard", base)
    counts: dict[str, int] = {}
    for row in results:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    (out / "results.json").write_text(json.dumps({"counts": counts, "rows": results}, ensure_ascii=False, indent=1))
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
