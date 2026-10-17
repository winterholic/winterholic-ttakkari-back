# Muse 사용량 새로 조회 및 관측값 수집 보완

로그인 표시 수정(21acf68)만으로 사용량은 표시되지 않았다. 조사 시작 시 운영 `/api/version`은 1e1556d였다. `muse serve`의 `usage/read`는 호스트의 마지막 관측값을 읽을 뿐이며, 새 호스트는 빈 값을 반환했다. `/usage`를 공식 터미널에서 실제 실행했을 때 처음에는 Power Usage 플랜과 Currently unavailable이 나왔다. 이를 인증 실패나 0%로 바꾸지 않았다.

같은 구독 프로필로 `muse exec --json --model muse-spark-1.3 --contributor-mode off --preset empty --disable-shell --disable-write --disable-web-tools --disable-reminders --disable-request-user-input --no-foreign-personal-context --approval-judge off --max-model-steps 1 --no-session-log 'Reply with exactly OK. Do not call any tools.'`를 한 번 실행했다. 환경의 META_API_KEY·MUSE_AUTH_PATH·MUSE_TRANSPORT_TRACE는 제거했다. native 출력의 model은 muse-spark-1.3, stream_succeeded, run.terminal.completed의 text는 OK, exit 0이었다. 이는 중첩 Run의 최소 구독 모델 응답 검사이며, 정식 개발 어댑터·코드 수정·테스트·MCP·Wonder Full 권한의 검증은 아니다. sandbox를 끄거나 종량제 API로 바꾸지 않았다.

이후 native `/usage`에서 Power Usage / Current 0% / Weekly 0% 및 초기화·관측 시각을 실제로 받았다. SDK의 새 호스트 usage/read는 여전히 비어 있었다. native UI가 수치를 받지 못하던 정확한 공급자 원인은 미확정이며, 최소 모델 호출 전후의 관측 차이만 확인했다.

수정:

- SDK가 인증 성공·관측 없음이면 단일 스레드 Python helper에서 native `/usage`를 읽는다. pexpect가 PTY·프로세스를 관리하고 pyte가 터미널을 해석한다. 백엔드의 executor 스레드에서 forkpty를 호출하지 않는다.
- 18초 제한, 시작 시 계정 로딩 지연에만 최대 3회, 빈 임시 작업공간, 셸·쓰기 도구 비활성화, 모델 프롬프트 없음. 인증·결제·승인 질문에는 응답하지 않는다. native CLI가 자기 인증을 처리하며 비밀 파일·키체인·브라우저 쿠키를 따까리가 추출하지 않는다.
- 정수 퍼센트·분 단위 시각의 native UI 호환 파서다. UTC/C locale을 고정하며 형식 변경·미제공·권한 거절은 실패로 알린다. 이 경로는 안정적인 공식 JSON API라는 주장이 아니다. CLI 변경 때 실물 재검증이 필요하다.
- account-wide usage/changed가 세션 ID 필터에서 버려지던 bridge와 파서 누락을 수정하고 RunManager의 usage sink로 연결했다.
- 마지막 관측값은 플랜·원래 시각과 함께 저장한다. 빈 조회가 이를 지우거나 새로운 측정으로 갱신하지 않는다. 실패 시 기존 수집기 backoff를 적용하며 로그아웃·프로필 변경은 캐시를 폐기한다.

실환경 근거 범위:

- 후보 수집기 전체(SDK → native helper → 정규화)에서 실제 구독 5시간 0%·주간 0%와 Power Usage, error=null을 확인했다. 인증 config는 기존 muse-default를 사용했고 data/cache만 현재 Run의 허용된 진단 경로였다. 인증 정보를 복사하지 않았다.
- 원래 운영 프로필의 data/cache 경로를 그대로 쓰는 현재 중첩 Run 검사에서는 native helper가 permission_denied를 보고했다. 그 경로로 재시도하거나 sandbox를 우회하지 않았다. 이는 해당 Run의 결과이며 운영 백엔드에서도 거절된다는 증거가 아니다.
- 운영 백엔드는 배포 후 별도 검증 대상이다. 배포 예약 수락을 운영 사용량 API·화면 성공으로 보고하지 않는다.

검증: 신규 이벤트 누락·빈 조회 보존·로그아웃 검사 실패를 먼저 확인한 뒤 수정했다. native 새로 조회 호출 검사도 기존 경로에서 실패한 뒤 통과했다. 별도 테스트 DB ttakkari_muse_usage_fix_20261017에서 API·기존 Claude/Codex Run·계정·수집기 회귀를 실행했다. 재로딩 시 원래 관측 시각 유지, account-wide 이벤트의 RunManager → DB 저장, 윤년·연도 경계, 미제공/형식 변경, 인증 질문 무응답·자식 회수를 검사한다. 최종 명령·결과와 실제 수집 원문은 outbox a343d402-a591-4b66-b244-c662c28654e3에 남긴다. 개발 실행 활성화 플래그와 기존 계정 설정은 변경하지 않았다.
