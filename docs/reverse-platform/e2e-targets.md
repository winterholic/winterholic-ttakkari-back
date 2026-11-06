# 독립 Reverse E2E 원본 대상

이 문서는 실제 플랫폼 ReverseJob 새 에이전트가 관찰할 API·웹 원본의 실행 방법과 허용 범위를 고정한다. 여기 있는 서버와 검증은 대상 준비 상태일 뿐, 플랫폼 에이전트가 별도 작업 폴더에서 원본을 관찰하고 독립 구현을 비교하기 전에는 실제 agent E2E가 아니다.

## 사전 조건

저장소 루트에서 기존 `../back/.venv/bin/python`을 사용한다. 두 대상은 각각 별도 프로세스로 시작하고 `127.0.0.1`에만 바인딩한다. 시작 명령의 `--state-dir`에는 PID·소유 토큰 메타데이터만 기록된다. 해당 디렉터리를 실행별 임시 디렉터리로 지정한다.

## API 원본

시작:

```sh
TARGET_STATE="$(mktemp -d /tmp/reverse-e2e-api.XXXXXX)"
../back/.venv/bin/python scripts/reverse_e2e_targets.py start api --host 127.0.0.1 --api-port 0 --state-dir "$TARGET_STATE"
```

출력 JSON의 `url`, `port`, `pid`, `pid_file`, `environment`, `ready`를 확인한다. 포트 `0`은 OS가 고른 실제 포트로 바뀐다. `url`은 `/items` 관찰 주소다. 동일 포트의 `/state`와 `/large`도 직접 관찰할 수 있다. `/items` 값은 프로세스 메모리에만 있으며 새 프로세스를 띄우면 `count=0`, 빈 목록으로 초기화된다.

API 계약:

| 요청 | 기대 결과 |
| --- | --- |
| `GET /items` | `200`, 현재 항목 목록. 처음에는 `[]` |
| `POST /items` body `{"value":"alpha"}` | `201`, `{accepted:true,value:"alpha",count:1}`; 목록과 count 증가 |
| `GET /state` | `200`, `{count:n}` |
| `GET /large` | `200`, 길이 8192인 `payload` |
| 정의되지 않은 GET 또는 POST 경로 | `404`, `{error:"not_found"}` |
| `POST /items` 잘못된 JSON | `400`, `{error:"invalid_json"}` |
| `POST /items` JSON 객체에 `value` 없음 또는 객체가 아님 | `422`, `{error:"value_required"}` |

이 원본은 [기존 API 데모 핸들러](../../scripts/reverse_compare_api_demo.py)의 `/state`, `/large`, JSON 오류와 상태 증가를 상속하고 `/items` 목록을 더한다. 인증은 없고 오직 loopback 요청만 허용한다.

## 웹 원본

시작:

```sh
WEB_STATE="$(mktemp -d /tmp/reverse-e2e-web.XXXXXX)"
../back/.venv/bin/python scripts/reverse_e2e_targets.py start web --host 127.0.0.1 --web-port 0 --state-dir "$WEB_STATE"
```

출력 JSON의 `url`을 브라우저에서 연다. 포트 `0`은 실제 포트를 선택한다. 앱은 한 HTML 원본만 제공하고 외부 리소스를 요청하지 않는다. 데스크톱 화면과 폭 390px에서 원본을 관찰한다.

관찰할 동작:

- 수량 필드는 정수 1–5 범위이며, 빈 값·0·6·소수를 제출할 때 브라우저 입력 검증을 관찰한다.
- 유효한 수량을 제출하면 성공 상태와 제출 건수가 변한다. 연속 제출에서 건수가 누적된다.
- 수량 변경과 제출 결과가 사용자 이벤트 목록에 추가된다.
- 상태 메시지는 `aria-live`로 표시된다. 390px에서 가로 넘침, 패널·입력·버튼의 배치와 여백을 확인한다.
- 기능 비교와 시각 비교를 별도로 기록한다. 원본 스크린샷과 에이전트 구현 스크린샷은 같은 브라우저·뷰포트·상태로 비교한다.

## 종료

시작 JSON에서 `pid_file`을 복사해 해당 대상만 종료한다. 종료 명령은 기록된 PID와 소유 토큰을 제어용 loopback 포트에서 확인하고 원본 프로세스에 정상 종료를 요청한다. 토큰이 맞지 않거나 프로세스가 응답하지 않으면 다른 PID를 종료하지 않고 실패한다.

```sh
../back/.venv/bin/python scripts/reverse_e2e_targets.py stop --pid-file "$TARGET_PID_FILE"
```

API와 웹은 각자 출력된 `pid_file`로 각각 종료한다. 응답 JSON에서 `stopped: true`를 확인하고, 상태 디렉터리는 필요한 종료 증거를 확인한 뒤 사용자가 아닌 실행 담당자가 정리한다. daemon·launchctl·서비스로 설치하지 않는다.

## 실제 플랫폼 새 작업에 전달할 설명

> 실제 ReverseJob E2E를 수행하세요. API 원본은 `{API_URL}`이며 허용 관찰 경로는 `GET /items`, `POST /items`, `GET /state`, `GET /large`, 정의되지 않은 경로입니다. 웹 원본은 `{WEB_URL}`이며 브라우저에서 입력 검증, 유효 제출 뒤 수량과 누적 건수 상태, 수량 변경·제출 사용자 이벤트를 관찰하고 390px 반응형과 시각 상태를 기록하세요. 두 원본의 허용 범위는 `127.0.0.1`에 대한 HTTP/브라우저 GET 및 API 계약에 적힌 `/items` POST뿐입니다. 원본 상태 변경은 이 임시 API 안에서만 수행하세요. 비밀·인증 정보, 외부 시스템·네트워크, 운영 DB는 접근하지 마세요. 원본을 관찰한 다음 에이전트의 별도 작업 폴더에서 API와 웹을 독립 구현하고 각각 기능·오류·상태 전이 및 웹 시각 결과를 비교하세요. 원본 HTML이나 핸들러를 복사하거나 가져와 구현에 재사용하지 마세요. 특히 원본과 복제를 같은 코드로 만든 비교는 성공으로 세지 마세요. 원본·복제의 코드 출처, 작업 폴더, 요청/이벤트, 응답·상태, 브라우저 뷰포트와 스크린샷을 근거로 남기고, 둘 다 끝나면 PID 파일의 대상 서버를 종료하세요.

`{API_URL}`과 `{WEB_URL}`은 실제 시작 JSON의 주소로 바꾼다. 주소·PID 파일은 시작할 때마다 새로 발급된다.

## 준비 검증과 제한

프로세스 수준 API 테스트는 실제 HTTP 요청으로 성공·오류·경계·상태 누적을 확인하고, 웹 응답에서 관찰 기능 표식을 확인한다.

```sh
../back/.venv/bin/python scripts/test_reverse_e2e_targets.py
```

이 검증은 원본 준비 검사다. 플랫폼의 새 ReverseJob 에이전트가 별도 작업 폴더에서 코드를 만들고 실제 비교를 한 증거는 아니며, 브라우저 동작·화면 캡처도 자동으로 대신하지 않는다.
