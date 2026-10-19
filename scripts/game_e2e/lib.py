"""격리 따까리(API 18790, 게임 origin 18795, front 5410)를 상대로 G14 단계를 수행하고 증거 JSON 에 남기는 보조 스크립트."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, "/private/tmp/gs-back/tests")
from ttakkari.security.auth import issue_access_token  # noqa: E402

OUT = Path("/Users/winterholic/.ttakkari/data/outbox/66e4a86d-7d4b-4a14-aa50-f470b3494bc3/g14")
OUT.mkdir(parents=True, exist_ok=True)
EVID = OUT / "g14-evidence.json"
API = "http://127.0.0.1:18790"
FRONT = "http://127.0.0.1:5410"
PW_DIR = "/Users/winterholic/development/projects/winterholic-ttakkari/back/.browser-runtime"
SCEN = Path("/private/tmp/gs-back/tests/game_smoke")


def evidence() -> dict:
    return json.loads(EVID.read_text())


def save(ev: dict) -> None:
    EVID.write_text(json.dumps(ev, ensure_ascii=False, indent=1))


def client() -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {issue_access_token()[0]}"}, timeout=120)


def git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout.strip()


def wait_build(c: httpx.Client, gid: str, bid: str, timeout: int = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        b = c.get(f"/api/games/{gid}/builds/{bid}").json()
        if b["status"] != "building":
            return b
        time.sleep(2)
    raise TimeoutError("build")


def start_build(c: httpx.Client, gid: str) -> dict:
    r = c.post(f"/api/games/{gid}/builds", json={})
    print("build start", r.status_code, r.text[:300])
    r.raise_for_status()
    return wait_build(c, gid, r.json()["id"])


def play(c: httpx.Client, gid: str, build_id: str | None = None) -> dict:
    r = c.post(f"/api/games/{gid}/play", json={"build_id": build_id} if build_id else {}, headers={"Origin": FRONT})
    r.raise_for_status()
    return r.json()


def node(script: str, cfg: dict, timeout: int = 420) -> dict:
    cfg = {"playwright_dir": PW_DIR, **cfg}
    p = subprocess.run(["node", str(SCEN / script), json.dumps(cfg)], capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("@@RESULT@@")]
    if not lines:
        print(p.stdout[-1500:], p.stderr[-1500:])
        raise SystemExit("no result")
    return json.loads(lines[-1][len("@@RESULT@@"):])


def rules(c: httpx.Client, gid: str, tag: str, build_id: str | None = None, mobile: bool = False) -> dict:
    pl = play(c, gid, build_id)
    return node("e2e_roguelike.mjs", {"play_url": pl["url"], "sandbox": pl["sandbox"], "host_port": 0, "host_origin": FRONT, "out_dir": str(OUT),
                                      "tag": tag, "mobile": mobile})


def sha(p: str) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


if __name__ == "__main__":
    ev = evidence()
    gid, root = ev["game"]["id"], ev["game"]["root"]
    cmd = sys.argv[1]
    with client() as c:
        if cmd == "build":
            name = sys.argv[2]
            b = start_build(c, gid)
            ev["steps"].setdefault("builds", {})[name] = {"id": b["id"], "seq": b["seq"], "status": b["status"], "source_commit": b["source_commit"],
                                                           "bundle_sha256": b["bundle_sha256"], "lock_verified": b["lock_verified"], "error": b["error"],
                                                           "head": git(root, "rev-parse", "HEAD"), "log_tail": (c.get(f"/api/games/{gid}/builds/{b['id']}").json().get("log_tail") or "")[-600:]}
            save(ev)
            print(json.dumps(ev["steps"]["builds"][name], ensure_ascii=False)[:1200])
        elif cmd == "rules":
            tag, mobile = sys.argv[2], len(sys.argv) > 3 and sys.argv[3] == "mobile"
            res = rules(c, gid, tag, mobile=mobile)
            ev["steps"].setdefault("rules", {})[tag + ("-mobile" if mobile else "")] = res
            save(ev)
            print(json.dumps({"checks": res.get("checks"), "error": res.get("error"), "notes": res.get("notes"), "console": res.get("console")}, ensure_ascii=False, indent=1))
