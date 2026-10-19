import asyncio, json, subprocess
from pathlib import Path
import httpx
from ttakkari.security.auth import issue_access_token
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.contract import ProjectManifest, game_dir

OUT = Path("/Users/winterholic/.ttakkari/data/outbox/66e4a86d-7d4b-4a14-aa50-f470b3494bc3/g14")
EVID = OUT / "g14-evidence.json"
token, _ = issue_access_token()
H = {"Authorization": f"Bearer {token}"}
API = "http://127.0.0.1:18790"
evidence = json.loads(EVID.read_text()) if EVID.exists() else {"steps": {}}

async def main():
    slug = "dot-dungeon"
    evidence["steps"]["2_stack"] = {
        "chosen": "web-phaser (Phaser 3.90 + Vite, 브라우저 2D)",
        "reasons": [
            "현재 구현된 엔진 어댑터는 Phaser 하나뿐이다(ADR 0046). 격리 빌드·bridge·계측이 모두 이 템플릿 기준이다.",
            "2D 로그라이크(방 이동·적·공격·체력)는 타일/스프라이트 2D 로직이라 Phaser 로 충분하다.",
            "모바일 PWA 브라우저에서 터치로 바로 실행된다(웹 번들, 설치 불필요).",
        ],
        "rejected": {"godot": "어댑터 없음(Phase 4)", "three.js": "3D 불필요, 어댑터 없음(Phase 4)"},
    }
    manifest = ProjectManifest(id=slug, name="도트 던전", genre="로그라이크", engine="web-phaser", version="0.1.0", entrypoint="index.html",
        locked_decisions=("아트 스타일: 도트(픽셀 아트). 외부 에셋 없이 코드로 만든 픽셀 스프라이트만 사용",
                          "2D 탑뷰 로그라이크 한 판짜리(방 3개), 모바일 터치 우선 + 키보드 지원",
                          "게임 엔진: Phaser 3 (web-phaser 템플릿)"))
    root = game_dir(slug)
    await PhaserAdapter().scaffold_project(root, manifest)
    async with httpx.AsyncClient(base_url=API, headers=H, timeout=60) as c:
        r = await c.post("/api/games", json={"slug": slug}); r.raise_for_status(); game = r.json()
        a = await c.post(f"/api/games/{game['id']}/lock/approve"); a.raise_for_status(); game = a.json()
    evidence["game"] = {"id": game["id"], "slug": slug, "workspace_id": game["workspace_id"], "root": str(root)}
    evidence["steps"]["3_project_and_lock"] = {"game": game, "scaffold_commit": subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()}
    EVID.write_text(json.dumps(evidence, ensure_ascii=False, indent=1))
    print(json.dumps(evidence["game"]), game["genre"], game["lock_approved"], game["locked_decisions"])
asyncio.run(main())
