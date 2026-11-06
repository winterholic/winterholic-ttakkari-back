# Reverse 통합 고정 revision 독립 감사 — 18917626

## 판정

**지정된 DB 비의존 완료 판정·release 경로·FIFO 범위는 통과했다.** 구현자와 검증자 역할의 결과는 따로 남고, verifier의 `passed`만으로 구현 실패가 `done`이 되지 않았다. 개발 checkout과 `release/<sha>/.venv` 모의 배치에서 두 capture/verify CLI 명령이 모두 실행됐다. 이 결과는 실제 DB/API 저장·dispatch, 운영 브라우저 E2E, 브라우저 provenance를 검증하지 않는다.

## 실행 신원과 기준

- 신원 기록 정정: 실제 감사 Run은 `861fee24-24a1-423b-9069-d11d363eb67b`, 보완 Run은 `af23fdfe-304d-4420-bfde-193672c3d0e9`다. 기획이 지원 API의 각 Run.profile을 `/api/accounts`의 유일한 동일 이름 계정과 연결해 실제 ID `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`와 enabled 상태를 확인했다. 이전 문서 및 b3cdd013 보완의 `6b76a5c4-a622-4345-b7ba-52fd2be2e99f`와 “API 실제 ID와 일치한다”는 주장은 잘못된 기록이다. `f0cd8b8e`는 별도 과거 Run으로 이번 감사 증거에서 제외한다. 필터된 실제 API 값은 [기획 신원 대조](./lead-identity-check-01d2aeb2.json)에 보존했다. 이 조회는 API 반환 범위의 연결을 확인하며 그 밖의 인증 이력을 증명하지 않는다.
- 감사는 detached worktree `back-reverse-integrated-audit-20261107`를 기준 `18917626934ab38302bddfee1c7f4b9ebe6dde73`에서 만들었다. `f0a382ec`가 조상이고 `18917626`의 ZIP/release 교정이 그 다음 commit임을 확인했다. main 및 기존 통합 worktree는 읽기 전용이었다.
- 운영 revision은 지원 API `ttk get /api/version`으로 조회했다: `529022861120e86f80400ffaec53dd3f3f7f81c6`. 따라서 아래 release 배치는 임시 모의 배치이며, 고정 189 revision이 운영에 배포됐다는 주장은 하지 않는다.
- 최종 수정·커밋 대상은 `docs/reverse-platform/audit-integrated-18917626/`의 probe·출력·보고서뿐이다. mock release의 첫 fixture 구성에서 symlink 경로에 placeholder를 쓰며 감사 worktree의 `reverse_tasks.py`가 일시 변경된 것을 발견해, 즉시 고정 SHA blob으로 복원했다. 복원 후 제품 파일 SHA가 기준 blob SHA와 일치함을 확인했고 제품 파일은 커밋에 포함하지 않았다.
- 이 일시 제품 파일 덮어쓰기와 그때의 원래 범위 위반 사실은 보존한다. 보완 실행 fixture는 venv까지 임시 디렉터리에 실제 `copytree`로 복사하고 release·venv·interpreter의 실경로가 해당 임시 디렉터리 안이며 release/venv 경로가 symlink가 아님을 검사한다. 보완 실행의 전후 제품 SHA와 명령 출력은 [보완 기록](./supplement-20261107.md)에 남겼다.

## 완료 판정과 callback

`probe_webcore.py`는 고정 revision의 `on_finished`를 직접 호출하고 DB 쿼리 경계만 fake로 대체했다. 원본·복제 캡처는 synthetic이며 DB 저장이나 generation dispatch의 증거가 아니다.

| 반례·대조 | 검증 결과 |
|---|---|
| 구현자 기능 match + 시각 mismatch, repair 한도 0 | `partial`; verifier는 별도로 `passed`; repair 0회 |
| 구현자 시각 unverified, repair 한도 0 | `partial`; verifier는 별도로 `passed`; repair 0회 |
| 1회 repair 후에도 구현자 시각 mismatch | repair 1회 뒤 `partial`; verifier는 별도로 `passed`; 초기 구현 결과가 history에 유지됨 |
| CLI 및 API 구현 mismatch | 각각 `partial`; verifier의 별도 match는 완료를 승격하지 않음 |
| 구현 비교 0건, source 누락, execution check 누락 | 세 경우 모두 `partial` |
| 구현자와 verifier의 필수 직접·시각 근거가 모두 match | `done`; 두 report·비교·Run ID와 `independent_verification` 판정이 별도 보존됨 |
| analyze mode | `done`, 독립 검증은 `not_required` |
| 중복·늦은 callback | 호출 전후 status/result가 유지되고 중복 repair row가 추가되지 않음 |
| verifier 실패 후 repair | quota 안에서 repair가 생성되고 이전 verifier session이 정리된 뒤 새 verifier session/Run 생성 |

오래된 probe의 duplicate status 검사는 `done` 상수 비교였던 부분을 현재 status의 callback 전·후 비교로 고쳤다. detailed case values는 [`probe-webcore-output.json`](./probe-webcore-output.json)에 있다.

## Capture/verify 경로와 비정규 파일

`probe_zip_audit.py`는 공백이 포함된 임시 job 경로에서 prompt가 만든 명령을 실제로 호출했다. 개발 checkout 명령은 현재 interpreter, checkout `src`, 두 scripts 경로를 사용했다. 모의 release는 `releases/18917626934ab38302bddfee1c7f4b9ebe6dde73/.venv/bin/python`과 release 내부 `src`·두 scripts를 가진 별도 임시 복사본이다.

| 배치 | 역할 | capture | verify |
|---|---|---:|---:|
| 개발 checkout | implementation | exit 0 | exit 0 |
| 개발 checkout | verifier | exit 0 | exit 0 |
| release 모의 배치 | implementation | exit 0 | exit 0 |
| release 모의 배치 | verifier | exit 0 | exit 0 |

FIFO source, check log, web raw JSON, web PNG를 각각 별도 3초 제한 subprocess에서 넣었다. 네 프로세스 모두 timeout 없이 exit 0으로 ZIP manifest의 거부 사유를 반환했다. 사유는 source/log에서 `not a regular file`, raw/PNG에서 `일반 파일이 아닙니다.`였다. 상세 명령·stdout/stderr는 [`probe-zip-output.json`](./probe-zip-output.json)에 있다.

## ZIP 무결성 및 경계

구현자와 verifier ZIP을 각각 열고 CRC/member 중복을 확인한 뒤 추출했다. 역할별 Run/session 연결이 유지됐고 raw JSON, 원본·복제 PNG, 재계산 comparison, check log의 바이트 수·SHA-256이 manifest와 일치했다. 추출된 comparison을 다시 검산해 두 역할 모두 기능 `match`, 시각 `mismatch`, integrity `verified`, provenance `unverified`로 나왔다. 동일한 상대 PNG 경로의 중복은 두 역할 manifest에서 확인했다.

| 검사 | 결과 |
|---|---|
| 사후 PNG 변경 및 다른 역할 Run ID | ZIP에서 제외되고 사유 기록 |
| 경로 탈출·absolute path·symlink·`.env` 민감 경로 | 제외 |
| 일반 파일 1 MiB 정확 경계 / 1 MiB + 1 byte | 경계 파일 포함, 초과 파일 제외 |
| PNG 12 MiB 상한 | **기존 `efd15f92` ZIP 감사의 확인 결과 재사용**. 189의 변경은 prompt interpreter와 비정규 파일 open 경로이며 PNG 상한/검증 경로는 변경되지 않아 재실행하지 않았다. 기존 확인: 1 MiB 초과 PNG는 12 MiB 한도까지 허용, 12 MiB 초과는 제외. |
| 브라우저 provenance | 계속 `unverified`; 합성 캡처의 파일 무결성을 출처 인증으로 취급하지 않음 |

ZIP 내부 파일별 Run/session, 크기, SHA 및 비교 재계산은 출력 JSON의 `two_role_zip.inventory`와 `two_role_zip.recomputed`에 있다.

## 미검증 및 하지 않은 항목

- DB/API 저장, manager callback의 실제 transaction 및 generation dispatch: **미검증**. 지시대로 `initdb shmget` 재시도·우회는 하지 않았다.
- 운영 브라우저 ReverseJob E2E, RunEvent/tool-result 기반 provenance: **미검증**. 모의 release는 실행 경로 확인이며 운영 release 설치·캐시 쓰기·재시작이 아니다.
- PNG 12 MiB 경계: 위 표와 같이 변경 영향이 없어 기존 유효 감사 결과를 재사용했다.
- 이 문서의 최초 신원 기록 오기는 위 API 한정 확인 결과로 정정했다. 당초 인용한 Run과 일시 제품 파일 덮어쓰기라는 범위 위반을 삭제하거나 정당화하지 않았다.
- 제품 코드·구현자 테스트·기존 감사·다른 dirty 변경: **최종 커밋에 포함하지 않음**. mock fixture의 일시적인 `reverse_tasks.py` placeholder 쓰기는 기준 blob으로 복원했고 최종 SHA-256을 다시 대조했다.
- push 및 배포: **하지 않음**.

## 재현 및 정적 확인

고정 checkout에서:

```sh
PYTHONPATH=src ../back/.venv/bin/python docs/reverse-platform/audit-integrated-18917626/probe_webcore.py
PYTHONPATH=src ../back/.venv/bin/python docs/reverse-platform/audit-integrated-18917626/probe_zip_audit.py
../back/.venv/bin/python -m py_compile docs/reverse-platform/audit-integrated-18917626/probe_webcore.py docs/reverse-platform/audit-integrated-18917626/probe_zip_audit.py
git diff --check
```

최종 실행은 두 probe exit 0, `py_compile` exit 0, `git diff --check` exit 0이어야 한다. callback positive control은 `done`, 세 웹 실패는 모두 `partial`; dev/release mock capture·verify는 각 역할 exit 0; FIFO는 4/4 `timed_out=false`다.
