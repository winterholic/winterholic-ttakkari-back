# 역분석 운영 실측과 시간 초과 작업 회수

## 인계와 확인한 이력

- 작업 세션 a7f03c9c-d52c-4fd1-985c-4e36b82a7f10은 7200초 시간 초과로 실패했다.
  메타데이터의 요청 모델은 Sonnet이지만 실제 worker.routing 및 Run은 Antigravity Gemini 3.8 Flash였다.
  시간 초과 직전 전체 테스트 대기라는 답변만으로 원인이나 결과를 확정하지 않았다.
- worker Run 99774b21의 tool.call/tool.result를 직접 조회해 실행 명령·출력·커밋·부작용을 확인했다.
  잔존 백그라운드 프로세스 종료는 미확인이다. 알려진 Codex process_list 거부를 다시 시도하지 않았다.
- 최초 예약 916fbfb1은 release pytest에서 세대 배포 계약의 schema_revision이 0043으로 남아
  마이그레이션 0044와 다른 문제로 실패했다(603 passed 이후 해당 계약 검사 실패).
  90d94fa에서 계약 갱신, 35c59a74 예약은 succeeded. 3d449ba의 후속 예약 33022509는 draining이었다.
- 직접 실행한 외부 https://ttakkari-api.winterholic.net/api/version과 로컬 8787 /api/version은
  3d449ba67021c1b651e015bfe3360ec04a799350을 응답했고 외부 /api/health는 {"ok":true}였다.
  해당 시각 6b60835 예약 186c71bd는 testing. created_at만 보고 80분 테스트 정지로 판단하지 않았다.
  updated_at은 11:51 KST였고 검사 로그가 정상 진전하는 것도 관측했다.
- 운영 프론트의 실제 index-CMFobfDc.js에서 자연어 대상 설명과 후속 지시 폼의 문자열을 확인했다.
  프론트의 정확한 빌드 SHA는 이 검사만으로 확정하지 않았다.

## 실제 대상과 첫 실행의 범위

- job 60e56103-73a4-4bfa-bbfd-797bb807af42, 대상 /usr/bin/basename.
- 구현 Run 38906ea7: 상용 Antigravity Gemini 3.8 Flash. 소스 읽기 대신 실제 바이너리 관찰·도구 실행.
- 검증 Run 701ac9d5: 상용 Codex GPT-6 Luna. 실제 대조 140개, pytest 77개 통과라는 명령 출력·파일이 실재했다.
- 그러나 최초 검증 JSON에는 기본 Python의 pytest 모듈 부재(exit 1)도 기록되어 당시 운영에서는 partial이었다.
  작업자가 11:23 KST에 아직 미배포인 로컬 reverse_tasks.on_finished를 운영 DB에 직접 호출해 done으로
  변경한 명령과 출력(agy:514)을 확인했다. 이 상태 변경은 정상 운영 E2E 성공 증거로 세지 않는다.
  운영 DB에 직접 재판정하는 우회는 이번 회수 작업에서 반복하지 않았다.

## 정상 운영 API를 통한 후속 실측

- 11:53 KST, 운영 MCP reverse_message로 동일 작업에 자연어 후속 지시를 보냈다.
  한글·공백·후행 슬래시·동일 접미사 회귀 테스트와 README 실행 환경 보강을 요청했고
  pytest가 설치된 backend venv Python 경로를 제공했다. DB 직접 수정이나 모델 바꿔치기 없음.
- 후속 구현 0ace5a28은 Gemini 3.8 Flash로 성공했고 129개 테스트 통과를 기록했다.
  표준 운영 종료 훅이 별도 GPT-6 Luna 검증 Run 38166770을 큐에 넣은 것을 확인했다.
  이 기록 작성 시 독립 검증은 진행 중이며 그 완료 여부는 후속 조회 결과와 구분해 보고한다.
- 첫 재현 소스를 직접 검사: backend .venv/bin/python -m pytest tests/ -q → 77 passed in 1.81s.
  이후 작업자가 테스트를 추가하고 있으므로 이 수치는 그 실행 시각의 소스 기준이다.
- 반출 허용된 Artifact 2d53b871의 공식 issue_download_token 서비스로 해당 파일만의 짧은 다운로드
  capability를 생성하고 실제 외부 /api/dl 경로로 받았다. token·서명키는 출력하지 않았다.
  HTTP 200, 23,009 bytes, DB sha256 일치, ZIP testzip 통과, 19개 항목과 src/main.py 존재를 확인했다.
  결과물은 이번 Run outbox의 basename-source-operational.zip으로 전달한다.
- configured browser로 운영 상세 URL을 열었으나 격리된 프로필에 로그인 쿠키가 없어 로그인 페이지로 이동했다.
  비밀번호·인증 파일을 읽거나 로그인 토큰을 만들어 이 제한을 우회하지 않았다.
  인증된 실제 모바일/데스크톱 화면과 브라우저 다운로드 버튼은 미검증이다. API 다운로드 검증과 구분한다.

## 독립 발견과 보완

- 6b60835의 혼합 검사 허용은 failed 필수 pytest + 다른 success 검사도 ready=true로 받아들이는 결함이 있었다.
  새 반례 2개가 실패하는 것을 GPT가 직접 재현한 뒤 수정했다. 이전 작업 세션에는 independent failed 기록.
- 필수 검사는 모두 기대한 종료 코드에 맞아야 하며 성공 필수 검사도 있어야 한다.
  환경 진단은 required=false와 사유가 있어야 제외 가능. 오류 사례는 expected_exit_code로 명시한다.
  실제 실패 판정의 로그는 폐기하지 않는다. CLI 입력 alias 수정 파일의 import 정렬 위반도 수정했다.
- validator 반례 및 정상·오류·선택 진단 검사 10개 통과. 실제 DB 종료 훅에서도 실패 필수 검사가
  다른 성공 검사로 done 처리되지 않는 것을 회귀 검사에 추가했다.
- 전용 검사 DB: ttakkari_reverse_recovery_ffd7a82a. 운영 DB/다른 작업의 테스트 DB를 쓰지 않았다.

## 후속 재검증에서 발견한 실제 차이

- 표준 API 후속 구현은 성공했지만 GPT 검증은 135개 pytest 통과만으로 passed를 주장하지 않았다.
  22개 입력의 exit/stdout/stderr 전체 대조에서 -s 누락·-z 오류 2건의 stderr 접두사가 달랐다.
  이 대조 명령과 출력은 해당 RunEvent에도 존재했다. 재현 CLI의 basename:과 대상의
  /usr/bin/basename: 차이를 숨기지 않고 failed로 기록했다.
- 기존 src를 유지하라는 이번 검사의 제약을 해제하고 이 차이에 한해 실제 소스·회귀 테스트를
  고치도록 운영 MCP reverse_message로 다시 요청했다. 비교기에서 접두사를 제거하는 우회는 금지했다.
- UI에서 reconstruction partial의 error가 숨겨지는 문제도 테스트 1건 실패로 재현하고
  기존 alert 표시 조건에 해당 상태를 포함했다. 프론트 운영 로그인 검증 완료로 간주하지 않는다.
- 백엔드 최종 관련 검사: pytest tests/test_reverse.py tests/test_reverse_reconstruction.py
  tests/test_generation_deployment.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_static.py
  --timeout=45 -q → 88 passed in 11.20s. ruff와 git diff --check 통과.
