import asyncio, json
from pathlib import Path
import httpx
from ttakkari.security.auth import issue_access_token

OUT = Path("/Users/winterholic/.ttakkari/data/outbox/66e4a86d-7d4b-4a14-aa50-f470b3494bc3/g14")
EVID = OUT / "g14-evidence.json"
H = {"Authorization": f"Bearer {issue_access_token()[0]}"}
ev = json.loads(EVID.read_text())
IDEA = "2D 로그라이크 한 판짜리: 방 이동, 적, 공격, 체력, 게임오버/재시작"
SPEC = """사용자 아이디어: 2D 로그라이크 한 판짜리. 방 이동, 적, 공격, 체력, 게임오버/재시작이 있어야 한다.

구현 규칙(자동 검증이 아래 상태 값을 읽으므로 이름과 의미를 그대로 지킨다):
- src/main.ts 의 템플릿(Phaser 3 + src/bridge.ts)을 바탕으로 게임을 만든다. 필요하면 src/ 안에 파일을 나눠도 된다. bridge.ts 는 수정하지 말고 import 해서 쓴다.
- 도트(픽셀 아트) 스타일: 외부 이미지·사운드 에셋을 쓰지 말고, 코드로 픽셀 스프라이트 텍스처를 만들어(Phaser Graphics/Canvas 텍스처, pixelArt: true) 쓴다.
- 방은 3개(순서대로 방1, 방2, 방3). 플레이어는 방 안에서 이동한다. 방의 적을 모두 처치하면 오른쪽 문이 열리고, 문에 닿으면 다음 방으로 이동한다. 마지막 방(방3)의 적을 모두 처치하면 승리 화면.
- 조작: 방향키 또는 WASD 로 이동, 스페이스 키로 공격. 터치: 화면을 누르고 있는 방향으로 이동하고, 화면 오른쪽 아래의 큰 '공격' 버튼을 누르면 공격한다(터치 버튼은 이동 입력과 겹치지 않게).
- 공격: 플레이어 주변 반경 56px 안의 적 모두에게 1 피해를 준다. 공격 쿨다운 300ms. 적은 체력 2.
- 적: 플레이어를 향해 쫓아온다(속도는 플레이어 최고 속도의 60% 이하). 접촉하면 플레이어 체력 1 감소 후 800ms 무적.
- 플레이어 체력 3. 0 이 되면 게임오버 화면('GAME OVER'와 다시 시작 안내)이 뜨고, R 키 또는 화면 탭으로 처음부터 다시 시작한다.
- 화면 위에 체력(하트 또는 숫자), 현재 방 번호, 처치 수를 보여 준다.
- 매 프레임 publishState(...) 로 아래 모양의 값을 게시한다(좌표는 게임 화면 픽셀):
  { scene: 'play' | 'gameover' | 'victory', room: 1~3, hp: 숫자, maxHp: 3, kills: 누적 처치 수, doorOpen: 불리언,
    player: {x, y}, enemies: [{x, y, hp}], w: 게임 너비, h: 게임 높이, door: {x, y}, attackReady: 불리언 }
  scene 이 'gameover' 면 enemies 가 비어 있어도 된다. 재시작 후에는 scene 'play', room 1, hp 3, kills 0 으로 돌아간다.
- 모바일 세로 화면(390x844)과 데스크톱 800x600 모두에서 한 화면에 방이 다 보이게 한다(Phaser.Scale.FIT 또는 RESIZE 후 방을 중앙 배치).
- 게임 로직이 끝나면 의미 있는 단위로 git commit 한다(커밋 메시지는 한국어). 호스트에서 npm 을 실행하지 말고, 빌드 검증은 플랫폼의 격리 러너가 한다. 소스만 정확히 작성하고 TypeScript 타입 오류가 없게 주의한다(빌드는 tsc --noEmit 를 먼저 돈다)."""
async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18790", headers=H, timeout=60) as c:
        r = await c.post(f"/api/games/{ev['game']['id']}/requests", json={"kind": "develop", "instruction": SPEC})
        print(r.status_code, r.text)
        r.raise_for_status()
        req = r.json()
    ev["steps"]["1_idea"] = {"input": IDEA}
    ev["steps"]["4_worker_develop"] = {"request": req, "instruction": SPEC}
    EVID.write_text(json.dumps(ev, ensure_ascii=False, indent=1))
asyncio.run(main())
