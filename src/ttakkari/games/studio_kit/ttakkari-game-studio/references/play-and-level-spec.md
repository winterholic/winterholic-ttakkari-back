# 플레이 스크립트와 레벨 명세

## 플레이 스크립트 (`play --script s.json`)

```json
{
  "name": "stage1-move-right",
  "viewport": { "width": 960, "height": 540 },
  "steps": [
    { "op": "wait", "ms": 800 },
    { "op": "focus" },
    { "op": "key", "code": "ArrowRight", "holdMs": 600 },
    { "op": "shot", "name": "after-move" },
    { "op": "expect", "expr": "window.__studio.state().x > 120", "label": "오른쪽으로 이동" },
    { "op": "expectNoConsoleErrors" }
  ]
}
```

op: `wait{ms}` · `focus` · `key{code,holdMs}` · `keys{codes[],holdMs}` · `tap{x,y(0~1)}` · `eval{expr,as}` · `waitFor{expr,timeoutMs}` · `dump{expr,to}` · `shot{name}` · `expect{expr}` · `expectNoConsoleErrors`.

- `code` 는 물리 키(`KeyZ`, `ArrowRight`, `Space`). 한글 IME 에서도 같다.
- 게임 상태를 읽으려면 게임이 `window.__studio = { state(), level() }` 를 노출한다(개발·테스트 빌드에서만 노출해도 된다). 템플릿의 `publishState` 브리지가 있으면 그 값을 쓴다.
- 대상 열기: `--url http://…`, `--url file:///…/index.html`, `--serve dist`(정적 서버, 127.0.0.1). 샌드박스가 브라우저의 loopback 접속을 막으면 `file://` 를 쓴다.
- 결과는 `docs/studio/evidence/<name>-<phase>-<시각>/result.json` + 스크린샷. `--attach F001 [--phase before]` 가 원장에 증거로 붙인다.
- Playwright 는 `STUDIO_PLAYWRIGHT_DIR`(node_modules 가 있는 폴더) → 게임 저장소 → 현재 폴더 순으로 찾는다. 없으면 exit 3.

종료 코드: 0 통과 · 1 실패 · 2 사용법 · 3 환경 미지원(증거 아님).

## 레벨 명세 (`reach --spec level.json`)

```json
{
  "bounds": { "w": 960, "h": 520 },
  "player": { "w": 22, "h": 18, "ox": -11, "oy": 4 },
  "step": 8,
  "rooms": [
    {
      "id": "pantry",
      "solids": [{ "x": 300, "y": 120, "w": 64, "h": 40 }],
      "spawns": [{ "id": "from-hall", "x": 480, "y": 300, "from": "hall" }],
      "targets": [
        { "id": "cabinet", "kind": "interact", "x": 520, "y": 200, "reach": 40 },
        { "id": "door-hall", "kind": "door", "rect": { "x": 440, "y": 480, "w": 80, "h": 16 }, "reach": 24 }
      ]
    }
  ]
}
```

- 플레이어 충돌 사각형 = `(x+ox, y+oy, w, h)`. 게임의 실제 충돌 판정과 같은 값을 쓴다.
- 방이 하나면 `rooms` 없이 최상위에 `solids/spawns/targets` 를 둬도 된다.
- 잡는 것: 스폰이 충돌체·경계 안(진입하면 못 움직임), 스폰에서 닿을 수 없는 상호작용·문·출구, 작은 구덩이에 갇힘(`minCells`).
- 모르는 것: 움직이는 장애물, 점프·대시, 잠금 상태에 따른 문 개폐. 그런 조건은 방을 상태별로 나눠 명세한다.
- 명세 추출: 게임이 `window.__studio.level()` 로 solids·spawns·targets 를 내보내고 `play` 의 `dump` op 로 파일에 저장한다. 추출기는 게임마다 다르다.
