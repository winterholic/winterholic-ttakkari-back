"""G31 수동 E2E: 녹화 구간 프레임을 실은 피드백을 실제 worker 에게 보내 프레임 열람·수정·자동 빌드를 확인한다. pytest 가 수집하지 않는다.

격리 따까리(API·게임 origin 이 떠 있고 TTAKKARI_* 가 격리 값)에서 실제 worker 를 부른다. 운영 DB·.env·운영 게임은 쓰지 않는다.
사용: TTAKKARI_NO_ENV_FILE=1 (격리 env) .venv/bin/python tests/game_smoke/clip_bench.py <결과 폴더> [color|missing|all]
각 사례는 격리 게임(슬러그 clip-bench-<사례>)에 시간 기반으로만 움직이는 작은 Phaser 게임을 넣고 결함을 심는다.
실제 bridge record 로 녹화한 JPEG 전체를 픽셀 스캔해 결함이 보이는 프레임 2장 + 정상 2장을 골라 clip 으로 올린다.
판정: worker 가 clip-NN.jpg 를 Read 로 열었는지(Run events), 수정 뒤 같은 구간 재녹화에서 결함이 사라졌는지(픽셀), 자동 빌드가 성공했는지.
비용·시간을 결과에 남긴다. 결함이 녹화에 안 보이면 worker 를 부르지 않고 그 증거만 남긴다.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

from ttakkari.security.auth import issue_access_token

API = os.environ.get("TTAKKARI_G31_API", "http://127.0.0.1:18790")
HOST_PORT = int(os.environ.get("TTAKKARI_G31_HOST_PORT", "18796"))
GAMES_ROOT = Path(os.environ.get("TTAKKARI_GAMES_ROOT", "/private/tmp/gs-g16/games"))
HERE = Path(__file__).parent
BACK = HERE.parent.parent
BLOB = 200  # 이 개수 이상이면 그 색 공이 프레임에 있다고 본다(스캔 폭 320 기준)

CASES = {
    "color": {
        "fixture": "clip_bench_color.ts",
        "instruction": ("공이 화면을 가로지르는 동안 중간부터 색이 바뀌어 보여. "
                        "구간 내내 처음에 보이던 색 그대로여야 해. "
                        "프레임을 순서대로 열어 보고 어디서 어떻게 바뀌는지 확인하고 고쳐줘."),
        "note": "뒤쪽 프레임에서 공 색이 달라요",
        "marker": "0x3366ff",  # 수정 뒤에는 이 파란색이 소스에서 사라져야 한다(참고용. 1차 판정은 픽셀)
    },
    "missing": {
        "fixture": "clip_bench_missing.ts",
        "instruction": ("공 세 개가 함께 움직이는데 구간 뒤쪽에서 하나가 사라져. "
                        "프레임을 순서대로 열어 보고 어느 공이 사라지는지 찾아 고쳐줘."),
        "note": "뒤쪽 프레임에는 공이 두 개만 보여요",
        "marker": "setVisible(bx <= 0.5)",
    },
}
TERMINAL_RUN = ("succeeded", "failed", "cancelled", "error")
TERMINAL_BUILD = ("succeeded", "failed", "cancelled")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=g31", "-c", "user.email=g31@local", *args],
                          capture_output=True, text=True, check=True).stdout


def headers() -> dict:
    return {"Authorization": f"Bearer {issue_access_token()[0]}"}


def wait_build(game_id: str, build_id: str, timeout: int = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        b = httpx.get(f"{API}/api/games/{game_id}/builds/{build_id}", headers=headers(), timeout=30).json()
        if b["status"] in TERMINAL_BUILD:
            return b
        time.sleep(3)
    raise SystemExit(f"빌드 {build_id} 가 끝나지 않았다")


def wait_last_build(game_id: str, after_seq: int, timeout: int = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        g = httpx.get(f"{API}/api/games/{game_id}", headers=headers(), timeout=30).json()
        b = g.get("last_build") or {}
        if b.get("seq", 0) > after_seq and b.get("status") in TERMINAL_BUILD:
            return b
        time.sleep(5)
    raise SystemExit("자동 빌드가 끝나지 않았다")


def scan(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    img = img.resize((320, round(img.height * 320 / img.width)))
    px = img.load()
    red = blue = green = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r >= 150 and g <= 130 and b <= 130:
                red += 1
            elif b >= 150 and r <= 130 and g <= 150:
                blue += 1
            elif g >= 150 and r <= 130 and b <= 150:
                green += 1
    return {"red": red, "blue": blue, "green": green}


def collect(game_id: str, build_id: str, out: Path, tag: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    host_origin = f"http://127.0.0.1:{HOST_PORT}"
    pw_dir = os.environ.get("TTAKKARI_PLAYWRIGHT_DIR") or str(BACK / ".browser-runtime")
    play = httpx.post(f"{API}/api/games/{game_id}/play", json={"build_id": build_id},
                      headers={**headers(), "Origin": host_origin}, timeout=30).json()
    cfg = {"playwright_dir": pw_dir, "host_port": HOST_PORT, "play_url": play["url"],
           "sandbox": play["sandbox"], "out_dir": str(out), "fps": 2, "ms": 6000, "tag": tag}
    proc = subprocess.run(["node", str(HERE / "clip_bench_collect.mjs"), json.dumps(cfg)],
                          capture_output=True, text=True, timeout=240, check=False)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("@@RESULT@@")), None)
    if line is None:
        raise SystemExit(f"수집 실패: {proc.returncode} {proc.stdout[-500:]} {proc.stderr[-500:]}")
    res = json.loads(line[len("@@RESULT@@"):])
    (out / "collect.json").write_text(json.dumps({k: v for k, v in res.items() if k != "files"}, ensure_ascii=False, indent=1))
    if res.get("error"):
        raise SystemExit(f"수집 오류: {res['error'][:500]}")
    assert res["started"]["supported"] and res["result"]["reason"] == "stopped" and res["jpeg"], res["result"]
    scans = []
    for f in res["files"]:
        s = scan(out / f["file"])
        scans.append({"file": f["file"], "t_ms": f["t_ms"], **s})
    (out / "scan.json").write_text(json.dumps(scans, ensure_ascii=False, indent=1))
    return {"collect": res, "scans": scans}


def pick_frames(case: str, scans: list[dict]) -> list[int]:
    """결함이 보이는 프레임 2장 + 정상 2장의 인덱스(시각순). 못 고르면 빈 목록."""
    if case == "color":
        ok = [i for i, s in enumerate(scans) if s["red"] >= BLOB and s["blue"] < BLOB]
        bad = [i for i, s in enumerate(scans) if s["blue"] >= BLOB and s["red"] < BLOB]
    else:
        ok = [i for i, s in enumerate(scans) if s["red"] >= BLOB and s["green"] >= BLOB and s["blue"] >= BLOB]
        bad = [i for i, s in enumerate(scans) if s["red"] >= BLOB and s["green"] < BLOB and s["blue"] >= BLOB]
    if len(ok) < 2 or len(bad) < 2 or ok[-1] > bad[0]:
        return []
    return sorted({ok[0], ok[-1], bad[0], bad[-1]})


def post_fixed(case: str, scans: list[dict]) -> bool:
    if case == "color":
        return all(s["red"] >= BLOB and s["blue"] < BLOB for s in scans)
    return all(s["red"] >= BLOB and s["green"] >= BLOB and s["blue"] >= BLOB for s in scans)


def run_case(case: str, out: Path) -> dict:
    spec = CASES[case]
    slug = f"clip-bench-{case}"
    r = httpx.post(f"{API}/api/games/create",
                   json={"idea": "구간 프레임 E2E", "name": f"구간 측정 {case}", "slug": slug,
                         "start_development": False, "create_pms": False}, headers=headers(), timeout=60)
    if r.status_code == 409:
        game = next(g for g in httpx.get(f"{API}/api/games", headers=headers(), timeout=30).json() if g["slug"] == slug)
    else:
        r.raise_for_status()
        game = r.json()["game"]
    root = GAMES_ROOT / slug
    (root / "src" / "main.ts").write_text((HERE / spec["fixture"]).read_text())
    git(root, "add", "-A")
    if git(root, "status", "--porcelain").strip():
        git(root, "commit", "-q", "-m", "clip bench fixture")
    base = git(root, "rev-parse", "HEAD").strip()
    row: dict = {"case": case, "slug": slug, "game_id": game["id"], "base": base[:12]}
    print(f"[{case}] game={game['id'][:8]} base={base[:12]}", flush=True)

    b = httpx.post(f"{API}/api/games/{game['id']}/builds", json={}, headers=headers(), timeout=60).json()
    build = wait_build(game["id"], b["id"])
    row["build_pre"] = {"id": build["id"], "seq": build["seq"], "status": build["status"], "error": build.get("error")}
    assert build["status"] == "succeeded", build.get("error")
    print(f"[{case}] build v{build['seq']} ok", flush=True)

    pre = collect(game["id"], build["id"], out / case / "pre", "pre")
    picks = pick_frames(case, pre["scans"])
    row["pre_frames"] = len(pre["scans"])
    if len(picks) != 4:
        row["verdict"] = "defect-not-visible"
        print(f"[{case}] 결함이 녹화에 안 보인다. worker 를 부르지 않는다", flush=True)
        return row
    picked = [pre["scans"][i] for i in picks]
    (out / case / "clip").mkdir(parents=True, exist_ok=True)
    arts = []
    for p in picked:
        src = out / case / "pre" / p["file"]
        shutil.copy(src, out / case / "clip" / p["file"])
        with open(src, "rb") as f:
            up = httpx.post(f"{API}/api/uploads", files={"file": (p["file"], f, "image/jpeg")},
                            data={"workspace_id": game["workspace_id"]}, headers=headers(), timeout=60)
        assert up.status_code == 201, up.text
        arts.append(up.json())
    clip = {"start_ms": picked[0]["t_ms"], "end_ms": picked[-1]["t_ms"],
            "frames": [{"artifact_id": a["id"], "t_ms": p["t_ms"]} for a, p in zip(arts, picked)], "note": spec["note"]}
    t0 = time.time()
    req = httpx.post(f"{API}/api/games/{game['id']}/requests",
                     json={"kind": "modify", "instruction": spec["instruction"],
                           "feedback": {"build_id": build["id"], "clip": clip}, "auto_build": True},
                     headers=headers(), timeout=60)
    assert req.status_code == 202, req.text
    req_json = req.json()
    (out / case / "request.json").write_text(json.dumps(req_json, ensure_ascii=False, indent=1))
    print(f"[{case}] run={req_json['run_id'][:8]}", flush=True)

    run: dict = {}
    end = time.time() + 1200
    while time.time() < end:
        run = httpx.get(f"{API}/api/runs/{req_json['run_id']}", headers=headers(), timeout=30).json()
        if run["status"] in TERMINAL_RUN:
            break
        time.sleep(4)
    t_run = time.time() - t0
    (out / case / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
    row |= {"run_id": req_json["run_id"], "engine": run.get("engine"), "model": run.get("model"),
            "run_status": run.get("status"), "cost_usd": run.get("cost_usd"), "seconds_run": round(t_run),
            "result_excerpt": (run.get("result_text") or "")[:2000]}
    prompt_clip = [ln for ln in (run.get("prompt") or "").splitlines() if "clip-" in ln or "프레임" in ln]
    (out / case / "prompt_clip.txt").write_text("\n".join(prompt_clip) + "\n")
    row["prompt_has_clip_files"] = sum(1 for ln in prompt_clip if "clip-0" in ln and ".jpg" in ln)

    events = httpx.get(f"{API}/api/runs/{req_json['run_id']}/events", headers=headers(), timeout=60).json()
    (out / case / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=1))
    reads = [e for e in events if e.get("type") == "tool.call" and "clip-" in json.dumps(e.get("payload", {}), ensure_ascii=False)]
    opened = sorted({m for e in reads for m in re.findall(r"clip-\d+\.jpg", json.dumps(e.get("payload", {}), ensure_ascii=False))})
    row["clip_read_seqs"] = [e["seq"] for e in reads]
    row["clip_tool_names"] = sorted({str(e.get("payload", {}).get("name", "?")) for e in reads})
    row["clip_files_opened"] = opened
    row["tool_calls"] = sum(1 for e in events if e.get("type") == "tool.call")
    row["guard_blocked"] = sum(1 for e in events if e.get("type") == "guard.blocked")

    head = git(root, "rev-parse", "HEAD").strip()
    row["committed"] = head != base
    row["diff_stat"] = git(root, "diff", "--stat", base, "HEAD").strip().splitlines()
    row["marker_gone"] = spec["marker"] not in (root / "src" / "main.ts").read_text()
    print(f"[{case}] {run.get('status')} cost={run.get('cost_usd')} opened={opened} committed={row['committed']}", flush=True)

    auto: dict = {}
    if run.get("status") == "succeeded" and row["committed"]:
        try:
            auto = wait_last_build(game["id"], build["seq"])
        except (Exception, SystemExit) as e:
            row["auto_build"] = {"status": "timeout", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        else:
            row["auto_build"] = {"seq": auto.get("seq"), "status": auto.get("status"),
                                 "source_commit": (auto.get("source_commit") or "")[:12], "error": (auto.get("error") or "")[:300]}
            print(f"[{case}] auto build v{auto.get('seq')} {auto.get('status')}", flush=True)
            try:
                reqs = httpx.get(f"{API}/api/games/{game['id']}/requests", headers=headers(), timeout=30).json()
                mine = next((x for x in reqs if x.get("run_id") == req_json["run_id"]), None)
                row["auto_build_result"] = (mine or {}).get("auto_build_result")
                row["lock_after"] = (mine or {}).get("lock_after")
            except Exception:
                pass
            if auto.get("status") == "succeeded":
                try:
                    post = collect(game["id"], auto["id"], out / case / "post", "post")
                except (Exception, SystemExit) as e:
                    row["post_error"] = f"{type(e).__name__}: {str(e)[:300]}"
                else:
                    row["post_frames"] = len(post["scans"])
                    row["fixed_pixels"] = post_fixed(case, post["scans"])
    opened_ok = len(opened) >= 1
    fixed_ok = row.get("fixed_pixels") is True
    build_ok = auto.get("status") == "succeeded" and (auto.get("source_commit") or "")[:12] == head[:12]
    row["verdict"] = "pass" if (opened_ok and fixed_ok and build_ok and run.get("status") == "succeeded") else "fail"
    print(f"[{case}] verdict={row['verdict']} (opened={opened_ok} fixed={fixed_ok} build={build_ok})", flush=True)
    (out / case / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=1, default=str))
    return row


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    arg = sys.argv[2] if len(sys.argv) > 2 else "all"
    cases = [arg] if arg in CASES else list(CASES) if arg == "all" else []
    if not cases:
        raise SystemExit(f"사례는 {sorted(CASES)} 또는 all. 받은 값: {arg}")
    rows = []
    for c in cases:
        try:
            rows.append(run_case(c, out))
        except (Exception, SystemExit) as e:
            rows.append({"case": c, "verdict": "error", "error": f"{type(e).__name__}: {str(e)[:500]}"})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    summary = {"cases": rows, "pass": sum(1 for r in rows if r.get("verdict") == "pass"), "total": len(rows)}
    (out / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({r["case"]: r["verdict"] for r in rows}, ensure_ascii=False))
    if summary["pass"] != summary["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
