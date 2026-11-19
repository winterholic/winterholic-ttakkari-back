# Context Sharing v2 공개 Reader 독립 검증

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 위치: `/Users/winterholic/development/projects/winterholic-ttakkari/back-context-sharing-v2-20261119`
- 시작/종료 HEAD: `978cb5ace5115743b32f375ce065120e8d068d82`
- 범위: 독립 Reader 테스트, `context_gateway/web/scripts/verify-share-v2/`, 본 기록만 추가했다. 제품 구현과 기존 테스트는 수정하지 않았다. 기존 작업 트리에 있던 다른 변경은 그대로 두었다.
- 검증 DB: `test_cg_v2_public_verify_20261119`, PostgreSQL `127.0.0.1:5432`, 사용자 `winterholic`. 실제 control/public 앱에서만 합성 문서와 자격을 발급했다.

## SHA 고정

| 파일 | SHA-256 |
|---|---|
| `context_gateway/web/src/share/ShareReader.tsx` | `35cd1368f56d1e97edeb5c55c8c2499c9853a605e89e924707cc2a04d98c5a32` |
| `context_gateway/web/src/share/ShareReader.test.tsx` | `73e6c11b595fdbf1fe7d6ad08b44da5693cf7b77cb6ca8129a845e1b3431ff3d` |
| `context_gateway/src/context_gateway/config.py` | `62a8d0c57d7370c2edd3caf47ee2fcfc4610c2b88335e198b52c79afe9c06b42` |
| `context_gateway/src/context_gateway/web_serving.py` | `92f9a35498f4e1791bb2e4840d0db9630b0ceddc3dbb034228f6705206751a3f` |
| `context_gateway/web/src/markdown/SafeMarkdown.tsx` | `b2215acd41085573a81bfccddc612c16b7d718a822c49d755b4670e713794fcd` |
| 검수 시 실제 제공한 빌드 JS | `43d74412b7c9a2a1e4880e0b64c1d693f9eb7446764291a3c911219e34713e9c` |

## 실행 근거

### 격리 DB와 실제 Gateway 앱

```text
$ /opt/homebrew/opt/postgresql@17/bin/createdb --no-password -h127.0.0.1 -p5432 test_cg_v2_public_verify_20261119
$ /opt/homebrew/opt/postgresql@17/bin/psql --no-password -h127.0.0.1 -p5432 -d test_cg_v2_public_verify_20261119 -tAc 'select current_database(), current_user, inet_server_addr(), inet_server_port()'
test_cg_v2_public_verify_20261119|winterholic|127.0.0.1|5432
```

정적 빌드와 독립 런처:

```text
$ npx tsc -p tsconfig.json --noEmit && npx vite build --outDir /private/tmp/context-gateway-public-verify-web-20261119
✓ 290 modules transformed.
✓ built in 66ms
$ CG_V2_PUBLIC_WEB_DIR=/private/tmp/context-gateway-public-verify-web-20261119 PYTHONPATH=context_gateway/src ../back/.venv/bin/python context_gateway/web/scripts/verify-share-v2/serve_fixture.py
READY origin=http://127.0.0.1:8747 database=test_cg_v2_public_verify_20261119 cases=primary,withdraw-link,expired ...
```

런처는 합성 문서를 실제 control API로 게시하고 `share_link_v2` 원문을 메모리에만 유지해, 브라우저의 진입 별칭을 `/s#token=...`으로 303 이동시켰다. 토큰은 로그·URL·검수 기록에 출력하지 않았다. 운영 데이터, 키, 공개 활성화, 배포는 사용하지 않았다.

HTTP probe:

```text
$ PYTHONPATH=context_gateway/src ../back/.venv/bin/python context_gateway/web/scripts/verify-share-v2/probe_http.py
HTTP PASS: exchange=200 repeat=200 cookie=Secure,HttpOnly,SameSite=Strict,Path context=200 export=200/read-only=404 cross-site=403 pause=503/resume=200 logout=200/session=401 expired=401 link-revoked=401 project-revoked=401
```

probe는 실제 로컬 control/public HTTP 경계에 대해 세션 쿠키 속성, 반복 교환, context/export scope, cross-site 거부, 일시 중단과 재개, logout 후 세션 차단, 만료·링크 폐기·프로젝트 폐기를 확인했다. 교환 응답·context·오류 응답에 링크 원문이나 세션 원문이 들어가지 않는 것도 검사했다. 쿠키는 검증 코드가 응답에서 속성만 판정한 뒤 원문 값을 출력하지 않고 같은 실제 HTTP API에 전달했다.

### 공개 Reader와 브라우저

- configured `mcp__ttakkari_browser`만 사용했다. 진입 alias `/__fixture-entry/primary`의 최종 URL은 `http://127.0.0.1:8747/s`였고, Reader가 fragment를 제거했다.
- 1280px와 390px viewport에서 프로젝트 이름·요약·문서·공유 만료·스냅샷 게시/생성/검증 시각·승인 출처를 확인했다. 390px에서 문서 목록과 출처 메타데이터가 세로로 표시됐다.
- 링크 URL을 반복 방문해 로그인 UI 없이 다시 읽었고, fragment 없는 `/s` 직접 재방문도 공유 쿠키로 `GET /api/v1/share/context => 200`을 확인했다.
- export scope 링크에서 Reader의 Markdown 다운로드를 눌러 실제 `/api/v1/share/export?format=markdown => 200`과 브라우저 다운로드를 확인했다. read-only 링크에는 다운로드 버튼이 없었으며, HTTP export 요청은 `404`로 거부됐다.
- 실제 링크 폐기와 만료 방문은 Reader의 만료/폐기 안내를 보였다. control에서 site pause 후 브라우저 진입이 실패하고, resume 후 primary 링크가 다시 열렸다.
- 악성 Markdown의 `<script>`와 `<img onerror>`는 실행 DOM 요소가 아니라 텍스트로 보였고 Markdown 이미지는 차단 안내로 렌더됐다. 네트워크 요청 목록에는 `tracker.invalid` 및 외부 자원 요청이 없었다.
- 정상 Reader 브라우저 콘솔 오류 0건. 의도한 401/503 거부 경로에서는 브라우저가 HTTP 실패 로그를 남겼고, 로그 내용에는 토큰이 없었다.
- 스크린샷(파일명 자동 저장):
  - `/Volumes/Backup/ttakkari-data/outbox/172a03b7-d1dd-420b-8f2b-b45166193054/browser/page-2026-11-19T10-12-40-557Z.png` — 1280px 전체 화면
  - `/Volumes/Backup/ttakkari-data/outbox/172a03b7-d1dd-420b-8f2b-b45166193054/browser/page-2026-11-19T10-13-38-494Z.png` — 390px 전체 화면

## 새 지연 반례와 기존 자동 검사

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx --reporter=dot --silent
Test Files  1 failed | 1 passed (2)
Tests       2 failed | 11 passed (13)
```

- 재현된 결함 1: 같은 Reader document에서 동일 원 링크 fragment를 연속 재개방하면 두 번째 교환 호출이 발생하지 않았다. 지연 반례가 두 번째 `/api/v1/share/exchange` 호출을 기다리다 실패했다. `activeToken`에 같은 값을 다시 설정할 때 React 상태 bailout으로 effect가 다시 실행되지 않는 가설이 이 재현에서 확인됐다.
- 재현된 결함 2: 서버 logout 요청이 네트워크 오류로 실패해도 Reader가 “이 브라우저에서 공유 세션을 종료했습니다”라고 표시했다.
- 경쟁 가설: 다른 링크의 context 응답을 지연시킨 동안 더 새 링크를 열고 먼저 새 응답을 받는 테스트는 통과했다. 늦은 context 응답이 최신 화면을 덮지 않았다.
- 기존 Reader 테스트 10개는 그대로 실행되어 통과했다. 새 두 결함은 작업 카드의 읽기 전용 경계에 따라 구현하지 않고 실패 증거로 남겼다.

## 보안 관찰 및 잔여 확인

- 실제 HTTP 교환 응답은 `Secure; HttpOnly; SameSite=Strict; Path=/api/v1/share`였다. 브라우저에서 교환 뒤 context 200, fragment가 없는 직접 `/s` 재방문 context 200을 확인했다.
- 브라우저 네트워크 요청은 same-origin 경로만 포함했고 token fragment가 요청 URL에 나타나지 않았다. 기존 Reader 자동 테스트의 `localStorage`/`sessionStorage` 비저장, `referrerPolicy: no-referrer`, DOM/오류 비밀 문자열 비노출 검사도 통과했다.
- `Referrer` 요청 헤더 값과 실제 브라우저 저장소 내용을 브라우저 도구에서 직접 덤프하지 않았다. 이 항목은 실제 브라우저 직접 관측이 아닌 기존 Reader 자동 테스트와 서버 `Referrer-Policy` 응답 헤더 근거다.
- 자체 앱은 `http://127.0.0.1:8747` loopback으로 제공했다. Gateway의 `Secure` 쿠키 속성은 유지됐고 configured 브라우저의 교환 후 context 요청이 200이었지만, 실제 TLS handshake는 이번 세션에서 확인하지 않았다.
- HTTP 응답에 `cache-control: no-store` 및 CSP/referrer 보안 헤더가 중복으로 관측됐다(예: `/api/v1/share/context` 응답에 같은 정책 헤더 두 벌). 해당 구현 파일은 소유 범위 밖이므로 수정하지 않았다. 값은 모두 no-store/no-referrer였으며 CSP 정책은 둘 다 적용됐다.
- TypeScript 패키지에는 `lint` script, ESLint 실행 파일/config가 없어 TS lint는 미실행이다. `tsc` typecheck와 Python 검증 스크립트의 Ruff lint는 실행했다.
- Gateway 전체/core 전체, Private 전체 회귀 및 관리 UI 브라우저 검증은 작업 카드 소유권 경계에 따라 하지 않았다.

검증 파일 정적 검사:

```text
$ npx tsc -p tsconfig.json --noEmit
exit 0
$ ../back/.venv/bin/python -m py_compile context_gateway/web/scripts/verify-share-v2/serve_fixture.py context_gateway/web/scripts/verify-share-v2/probe_http.py && ../back/.venv/bin/ruff check context_gateway/web/scripts/verify-share-v2 && git diff --check
All checks passed!
```

## 종료 및 정리

- configured 브라우저 `browser_close`: `No open tabs.`
- 자체 Uvicorn/Gateway 프로세스: 세션에 Ctrl-C를 보내 종료. StreamableHTTP session manager의 `shutting down` 로그를 확인했다.
- 자기 합성 DB만 제거:

```text
$ /opt/homebrew/opt/postgresql@17/bin/dropdb --no-password -h127.0.0.1 -p5432 test_cg_v2_public_verify_20261119
exit 0
```

- 임시 빌드·snapshot 폴더 및 `__pycache__` 정리는 command safety guard가 `rm -rf ...`를 거부해 미완료다. 관측 오류: `rm -f style commands are not permitted. Use a safer approach`. 같은 경로를 다른 명령이나 엔진으로 삭제 재시도하지 않았다. 남은 경로: `/private/tmp/context-gateway-public-verify-web-20261119`, `/private/tmp/context-gateway-share-v2-public-verify-20261119`, `context_gateway/web/scripts/verify-share-v2/__pycache__`.

## 최종 독립 재검수 — logout/exchange 경합과 쿠키 정합성

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 위치: `/Users/winterholic/development/projects/winterholic-ttakkari/back-context-sharing-v2-20261119`
- 시작/종료 HEAD: `978cb5ace5115743b32f375ce065120e8d068d82` (commit/push 없음)
- 변경 범위: 새 `context_gateway/web/src/share/ShareReader.final-acceptance.test.tsx`와 이 기록만 추가/갱신했다. `ShareReader.tsx`, `ShareReader.test.tsx`, `ShareReader.independent.test.tsx`는 읽기 전용이며 기존 반례를 완화하지 않았다.
- 기존 독립 반례와 Reader effect를 대조했다. 같은 문서 안에서 같은 fragment를 두 번 재개방하면 방문 횟수로 effect가 재실행되고 두 번째 context가 표시되는 기존 반례가 최종 집중 실행에서 통과했다.

### 실행 결과와 판정

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  1 failed | 2 passed (3)
Tests       3 failed | 17 passed (20)
```

- 기존 Reader 및 독립 테스트 17건은 모두 통과했다. 기존 범위에 지연 context가 새 링크 화면을 덮지 않는 검사, 지연 export의 저장 차단, logout 실패 안내 및 수동 재시도, 같은 링크 재개방 검사가 포함된다.
- 신규 반례 1 — 지연 exchange가 logout 응답 뒤 완료되고 보상 logout이 네트워크 실패하면 쿠키 상태는 `session-from-late-exchange`로 남았고 화면은 계속 “서버 공유 세션을 종료했습니다”라고 표시했다. 실패 상태/재시도 안내는 표시되지 않았다. 종료 성공 보장을 잘못 유지하는 재현이다.
- 신규 반례 2 — logout 응답을 지연시킨 사이 새 링크 exchange/context를 완료한 다음 늦은 logout이 성공하면 상태 있는 fetcher의 쿠키가 `session-new`에서 지워졌고 Reader는 새 링크 화면 대신 “서버 공유 세션을 종료했습니다” 화면을 보였다. 새 화면과 쿠키가 함께 보존되지 않는 역방향 경합이다.
- 신규 반례 3 — 구 링크 exchange가 새 링크보다 늦게 끝나 구 세션 쿠키를 설정하고, 보상 새 링크 재교환이 네트워크 실패하면 화면은 “새 링크 맥락”인데 fetcher 쿠키는 `session-old`였다. 이는 상태 있는 fetcher가 모사한 응답 Set-Cookie/삭제 순서에서 관측한 화면-쿠키 불일치다.
- 세 신규 테스트 실패는 수락 기대와 현재 코드의 차이를 확인한 결과이며, 구현 결함 여부는 운영 HTTP 관측으로 확대하지 않는다. 제품 코드 변경은 범위 밖이라 하지 않았다.

### 추가 검사와 식별자

```text
$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0
$ git diff --check
exit 0 (출력 없음)
```

| 파일 | SHA-256 |
|---|---|
| `context_gateway/web/src/share/ShareReader.tsx` | `358ea48e898ccb2845f8edebb88589d441fc88087bbcfeaa127976ddcaa927a2` |
| `context_gateway/web/src/share/ShareReader.test.tsx` | `54c718c48e33ed8982d8b4add3ecf87672322d8283d169b54fa12a1d94b738bb` |
| `context_gateway/web/src/share/ShareReader.independent.test.tsx` | `a078706c898cf5ec63e1a6e6bfb9207f57b0334274094b608c5387c55b39047b` |
| `context_gateway/web/src/share/ShareReader.final-acceptance.test.tsx` | `616159d97f12675a662c9fb9fbfcdceefc5a5f13b8b2446a841c6f4de12f4856` |

- 기존 실제 Gateway HTTP 및 390/1280px 브라우저 증거는 위의 기존 검수 기록 범위에서 재사용했다. 이 재검수는 비동기 요청 완료 순서와 쿠키 상태를 단위 fixture에서 확인했으므로 브라우저를 다시 열지 않았다. 운영 TLS, 실제 브라우저의 동시 Set-Cookie 순서, 전체 core/Private 회귀는 미실행이며 기존 증거로 새 경합 결과를 주장하지 않는다.
- 좁은 음성 대조는 최종 acceptance 세 테스트가 현재 Reader에서 실패하는 출력 자체로 확인했다. source 수정이 금지되어 GREEN 대조는 하지 않았다.

## 최종 수락 기준 교정과 재검증

- 일자: 2026-11-19 (Asia/Seoul)
- 범위: `ShareReader.final-acceptance.test.tsx`와 이 자기 검수 기록만 수정했다. 기존 두 테스트와 제품 파일은 이 작업에서 수정하지 않았다.
- 앞 절의 세 관측은 그 시점의 Reader/수락 테스트에서 나온 기록으로 보존한다. 이후 작업 중 Reader 파일 SHA가 이전 기록 `358ea48e898ccb2845f8edebb88589d441fc88087bbcfeaa127976ddcaa927a2`에서 바뀐 것을 관측했다. 현재 파일은 `sessionMutations` 직렬화와 logout 세대 확인을 포함한다. 변경 주체/시각은 이 검수에서 확인하지 않았고, 이 작업에서 제품 코드를 수정하지 않았다.

### 교정한 수락 규칙

- 현재 링크 내용을 보여 주려면 합성 fetcher가 추적한 쿠키가 해당 링크 세션이어야 하고 logout 성공 안내가 없어야 한다.
- 쿠키와 링크가 불일치하면 링크 내용을 숨기고 alert 안에 재시도 버튼이 있는 상태를 허용한다. logout 성공 문구와 보호 내용 숨김만으로는 새 링크 intent가 안전하다고 판정하지 않는다.
- 로그아웃 완료 상태는 보호 내용을 숨기고 추적 쿠키가 실제로 `null`이며 logout 2xx 성공 안내가 함께 있을 때만 허용한다. logout 요청 또는 exchange가 미완료인 동안에는 성공 문구를 미리 요구하지 않는다.
- 첫 테스트는 요청 횟수/보상 횟수를 기대하지 않는다. 두 번째·세 번째도 복구 exchange 성공이나 `session-new` 복원을 무조건 요구하지 않고, 일치 화면 또는 명시적 오류/재시도와 내용 숨김을 허용한다.
- 양방향 oracle 대조는 일치하는 권한 화면, fail-closed 오류, 쿠키가 지워진 종료 성공을 허용하고 구 쿠키 화면 및 쿠키가 남은 종료 성공을 거부한다.

### 현재 Reader 결과와 요청/쿠키 상태 추적

```text
$ npm test -- --run src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  1 passed (1)
Tests       6 passed (6)

$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  1 failed | 2 passed (3)
Tests       2 failed | 21 passed (23)
```

- 현재 Reader에서 새 acceptance 6건(상태 oracle 1, 경합 3, 정상 인증 및 fail-closed fixture 2)은 모두 통과했다. 기존 독립 테스트의 두 실패는 `늦은 이전 링크 exchange가 새 링크 쿠키를 덮으면…`가 늦은 exchange 해제 전에 새 context가 떠야 한다고 기다리는 부분과, `logout 뒤 완료된 지연 exchange…`가 logout 요청이 아직 직렬 큐에서 대기 중인데 종료 성공 문구를 요구하는 부분이다. 이 기존 파일은 범위에 따라 그대로 두었다.
- 직렬 경로의 합성 요청 추적: ① 지연 exchange 완료가 `session-from-late-exchange`를 설정한 뒤 queued logout 204가 이를 삭제한다. 결과는 내용 숨김 + 쿠키 없음 + 종료 성공(허용). ② 늦은 logout이 먼저 끝나 쿠키를 지운 다음 queued 새 링크 exchange가 `session-new`를 설정하고 새 내용을 반환한다(허용). ③ 구 exchange가 먼저 `session-old`를 쓰고 새 링크 exchange가 `session-new`로 교체한 뒤 context가 새 링크를 표시한다(허용). 이들은 상태 있는 fetcher가 적용한 Set-Cookie/삭제 순서이며 실제 브라우저/HTTP 관측은 아니다.
- 복구 실패 가지의 수락 oracle은 `session-old`와 새 링크 내용의 동시 표시를 거부하고, 보호 내용 없는 오류+재시도는 허용한다. 현재 직렬 경로는 해당 복구 실패 상태에 도달하지 않아 경합 테스트는 정상 직렬 완료 분기로 통과했다. 실패 네트워크 상태는 별도 fail-closed React fixture와 oracle의 반례로 검사했다.
- 판정: 현재 관측한 Reader SHA에서는 새 기준의 세 경합 테스트 RED가 재현되지 않았고 독립 acceptance는 GREEN이다. 반면 지정 세 파일 전체 실행은 기존 독립 테스트 2건이 직렬화 가정과 맞지 않아 실패한다. 그 두 테스트는 읽기 전용이라 조정하지 않았다. 이전 SHA의 세 실패 결과를 현 Reader SHA 결과로 간주하지 않는다.

### 재검증 SHA 및 검사

- Reader `context_gateway/web/src/share/ShareReader.tsx`: `f9ada7c05c571a89cd8ff234ce21e6351a4fbb656a7b68b4d1d6963dc63b9f31`
- 기존 Reader 테스트: `54c718c48e33ed8982d8b4add3ecf87672322d8283d169b54fa12a1d94b738bb`
- 기존 독립 테스트: `a078706c898cf5ec63e1a6e6bfb9207f57b0334274094b608c5387c55b39047b`
- 신규 acceptance 테스트: `bdd56d7fb7e97543305892a059db57512900651b5fa3222b002c373dbb020bdc`
- HEAD: `978cb5ace5115743b32f375ce065120e8d068d82` (커밋/푸시 없음)

```text
$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0
$ git diff --check
exit 0 (출력 없음)
```

- 운영 API, 브라우저, core/Private 전체 회귀, push 및 배포는 이 작업 카드에 따라 실행하지 않았다.

### 최종 지문 기준 재실행

- 직전 기록의 Reader SHA `f9ada7c0…`가 다시 변경되어, 마지막 두 실행 직후 `ShareReader.tsx`가 `59d34ac583c6d4f552e9e7f8568c98c7b3fd52e235ceb3cea8c79786850c0e79`로 확인됐다. 새 acceptance 파일 SHA는 `bdd56d7fb7e97543305892a059db57512900651b5fa3222b002c373dbb020bdc`, HEAD는 계속 `978cb5ace5115743b32f375ce065120e8d068d82`다. SHA 변화 주체는 확인하지 않았고 제품 파일은 수정하지 않았다.
- 현재 지문 집중 실행:

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  1 failed | 2 passed (3)
Tests       1 failed | 22 passed (23)
FAIL: 기존 ShareReader.independent.test.tsx의 지연 exchange 사례가 context 호출 2회를 기대했으나 3회 관측

$ npm test -- --run src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  1 passed (1)
Tests       6 passed (6)
```

- 이 최신 실행에서는 새 acceptance 6건이 전부 통과했고, 기존 테스트의 실패는 기존 assertion이 context 호출 수를 2로 고정한 지점이다. 기존 테스트는 범위상 변경하지 않았다. 앞 절의 `f9ada7c0…` 실행에서 관측한 기존 테스트 실패 두 건은 그 SHA 당시 결과이며, 마지막 현재 지문 실행 결과는 위와 같이 구분한다.
- `$ npm run typecheck` → `tsc -p tsconfig.json --noEmit` 성공. `$ git diff --check` 및 신규 acceptance/검수 기록 trailing-whitespace 검사 출력 없음.

## 독립 Reader 호출 수 실패 재판정 — 쿠키·토큰 요청 추적

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 범위: `ShareReader.independent.test.tsx`의 기존 지연 exchange 테스트 한 건, `ShareReader.final-acceptance.test.tsx`의 해당 수락 테스트, 본 기록만 수정했다. `ShareReader.tsx`와 기존 `ShareReader.test.tsx` 및 제품/Backend 파일은 수정하지 않았다.
- SHA 기준: 집중 재실행 직전과 모든 검증 뒤 Reader SHA-256은 `59d34ac583c6d4f552e9e7f8568c98c7b3fd52e235ceb3cea8c79786850c0e79`로 같았다. 이전 기록의 Reader 지문과는 분리된 결과다.

### 원래 실패의 원인

- 세 파일 실행에서 유일한 실패는 독립 테스트의 세 번째 context 요청 수를 무시하고 정확히 2회를 기대한 assertion이었다. 재현 출력은 `expected length 2 but got 3`였다.
- 현재 Reader는 오래된 `old-link` exchange가 새 링크 처리 뒤 끝나면 epoch 불일치를 감지한다. 그 뒤 `recoverCurrentIntent()`가 최신 `new-link`를 다시 exchange하고 context를 읽는다. 이는 늦은 응답이 덮어 쓴 세션 쿠키를 복원한 다음 보호 데이터를 조회하는 재확인이다.
- 기존 fetcher는 쿠키를 모델링하지 않고 “새 링크 완료 뒤 old exchange 완료”만 흉내 냈다. 따라서 3번째 호출이 발생한 원인과 그 호출의 권한이 올바른지는 기존 assertion이 검증하지 못했다. 호출 수는 사용자 보안 요구가 아니며, 이 추적에서 재확인 조회는 현재 링크 쿠키를 복구한 뒤 수행되어 안전했다. 확인된 호출 수만으로 제품 보안 결함이라고 판정하지 않는다.

### 강화한 수락 기준과 관측

- fetcher는 exchange token별로 쿠키 상태를 바꾸고 각 context 요청 당시 쿠키를 기록한다. 구 exchange가 `session-old`를 늦게 쓰는 순서를 재현한다.
- 관측 추적: 초기 context는 `session-initial`; `old-link` 완료 전 새 링크 exchange/context는 `session-new`; 늦은 `old-link` 응답 뒤 현재 `new-link` 재교환/context도 `session-new`였다. exchange 토큰 추적은 `[old-link, new-link, new-link]`, context 쿠키 추적은 `[session-initial, session-new, session-new]`다.
- 구 쿠키 `session-old`로 context를 요청하지 않았고, 이전 권한의 합성 content도 화면에 나타나지 않았다. 최종 화면은 새 링크 맥락이며 최종 추적 쿠키는 `session-new`다. 독립 테스트는 context 호출 횟수에 고정되지 않고 이 불변식을 단언한다.
- acceptance의 실패 복구 분기는 `old-link` 지연 완료 뒤 현재 링크 재교환이 실패하면 추가 context 요청을 보내지 않고 내용을 숨기며 재시도 버튼을 표시한다. 요청 trace의 context 쿠키는 `[session-initial, session-new]`; `session-old` 조회는 없다. 기존 양방향 상태 oracle은 잘못된 세션으로 내용이 보이는 상태와 쿠키가 남은 종료 성공을 계속 거부한다.

### GREEN 및 음성 대조

```text
$ npm test -- --run src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  2 passed (2)
Tests       13 passed (13)

$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files  3 passed (3)
Tests       23 passed (23)
```

- 정상 fixture의 GREEN 후 독립 테스트 fixture에서 두 번째 `new-link` exchange가 `session-old`를 잘못 유지하도록 잠시 바꿨다. RED 출력은 context 요청 추적이 예상 `session-new` 대신 실제 `session-old`를 관측해 실패했다. 이 임시 fixture 변이는 즉시 원복했고 제품 파일은 건드리지 않았다.
- 복구된 정상 fixture는 지정 세 파일과 전체 web 테스트를 다시 실행했다.

```text
$ npm test
Test Files  12 passed | 2 skipped (14)
Tests       120 passed | 5 skipped (125)

$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0

$ git diff --check
exit 0 (출력 없음)
```

### 최종 검증 SHA-256

| 파일 | SHA-256 |
|---|---|
| `context_gateway/web/src/share/ShareReader.tsx` (읽기 전용) | `59d34ac583c6d4f552e9e7f8568c98c7b3fd52e235ceb3cea8c79786850c0e79` |
| `context_gateway/web/src/share/ShareReader.test.tsx` (읽기 전용) | `54c718c48e33ed8982d8b4add3ecf87672322d8283d169b54fa12a1d94b738bb` |
| `context_gateway/web/src/share/ShareReader.independent.test.tsx` | `447d652c6d4b9e3b1bf26c57fcc90bc4a4fc169922e0f5bf53b5ccf2e5d08bf0` |
| `context_gateway/web/src/share/ShareReader.final-acceptance.test.tsx` | `66d462c9dc3a72eeea6ed99ad036968608b600744775f19720eab9a3650b501d` |

- 상태 있는 fetcher의 쿠키는 HttpOnly 브라우저 쿠키를 직접 덤프한 값이 아니라 테스트가 응답 Set-Cookie/삭제 순서를 모사한 합성 상태다. 운영 API, 실제 브라우저, 배포는 실행하지 않았다.
