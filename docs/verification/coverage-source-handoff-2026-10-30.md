# COVERAGE 소스 인계 — 2026-10-30

관측 시각: 2026-10-30 (KST). 목적은 기존 전수감사 판정 FAILED를 유지하면서, 이전 Run이 보호되어 열리지 않는 교정 파일을 대신해 승인 저장소의 출처와 대화 원문을 연결하는 것이다. 새 분모를 만들거나 완료를 판정하지 않았다.

## 근거 구분

- 승인 증거 원본: `/Users/winterholic/development/ttakkari-recovery-coverage-evidence-20261029/{inventory.tsv,verdict.md,recovered-requirements-20261029.tsv,coverage-correction-notes-20261029.txt}`. 원 100 목록은 복원되지 않았으며 현재 목록을 과거 분모로 대체할 수 없다.
- 대화 원문은 아래에 session ID와 speaker를 표시했다. 계획/worker 기록은 사용자 결정과 구분했다.
- 기획 `af14105d-230f-4ab2-908d-8a1fcae39d00`의 43+ 항목은 `read_session(last=40)` 및 승인된 coverage 매핑을 확인했다. 현재 Manage Work 원장은 지원 `manage_work` 도구로 직접 읽지 않았다. 여기서 추가 소유권 변경을 하지 않는다.

## 네 요구의 원문·ID·담당·남은 일

| 요구 | 검증된 원문·범위 | 원 session / Run | 기존 담당·관측 결과 | 남은 동작 |
|---|---|---|---|---|
| 회사 범위 에이전트 복구·권한 | 사용자 원문(회사 scope desk): “회사세션에서 작업이 불편할정도로 아예 아무것도 못하더라?? … 깃계정 스위칭도안됨 권한이막혀서;;” 요구는 회사 작업 편의와 표시 분리이며 회사 AI 계정 제한은 유지. 개인 계정으로 실행할 승인 아님. | session `a40b1284-f973-4679-952f-b4282b410b4a`; 관련 권한 변경 branch/commit `c8be9694` | 당시 담당 `INFRA 441ae34d`는 과거 소유자 prefix뿐이며 full ID는 회수하지 못함. 커밋은 `fix/recovery-work-permissions-20261028`에 게시됐으나 main 반영 및 DB gate는 해당 work-history에서 미완으로 기록. 현재 회사 scope 담당은 이 증거만으로 확정할 수 없음. | 별도 회사 scope 검토만. 개인 계정 실행 금지. root는 회사 레그나에 개입하지 않는다는 최신 지시 유지. |
| GitHub 계정 전환 / 403 | 사용자 원문: “양쪽 다 양쪽 깃계정을 모두 자유롭게쓸수있게하는게 난 좋은데?” (`974ac45d-430a-40b1-a270-ee884a12205f`, user). 앞선 요청은 `gh auth switch` 차단을 줄여 달라는 것이었다. 기록된 push 403이 `daeseon-dev`를 지목했다는 내용은 work-history/검토자 보고이지 이 원문 자체는 아님. | 원요구 session `974ac45d-430a-40b1-a270-ee884a12205f`; 구현 worker `5c4d37bd-62f6-4724-ade7-d2ed7244338c`; 독립 검토 `de0e5534-5df1-4d69-a1b6-19c4c45c9436` | 검토 결과 코드 경계는 부분 충족/교정 후 검토 통과로 기록됐지만 운영 allowlist 비어 있어 실제 활성화 미확인. 검토는 `gh auth token --user winterholic` 성공과 push 거부 응답의 계정 불일치를 적었고, 원인은 확정하지 않음. 현재 main 통합/게시의 기존 owner는 `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`; 이것이 계정 충돌 해결 증거는 아님. | fcb가 승인 경로의 계정 allowlist/main 반영 및 정상 push 경계를 확인해야 함. 자격증명 값을 읽거나 출력하지 않는다. |
| OpenCode/Copilot 엔진 검증 | 사용자 원문: “이거 일단 오픈코드에 연결해서 사용하는걸 메인으로 생각할거긴한데 … 디폴트 코파일럿cli도 호출가능하게 개발해주셈” (`f61dbaa4-adc0-460d-bd41-1652153d8ee4`, user turn `c8715933-2b26-4bef-9613-eca751ec18c0`). 이후 실제 응답·재개·MCP 실행 및 자동 배정을 요구한 원문은 같은 session의 user turn `54a82bcd-ad43-4306-b9a5-061802cc14b5`. | OpenCode 검증 task session `ecc176d9-1bdd-4f04-b6bf-6432477e51ee`; associated Run/full report ID `8cba5845-d62c-4adb-b2a2-4d0b6e63d586`; parent `f61dbaa4-adc0-460d-bd41-1652153d8ee4` | 최초 OpenCode live run은 `Unexpected server error`로 실패했고 root cause 미확정. 현재 기존 verifier `7d00facb-f484-4295-80cc-52f52343d514`는 root 최신 지시상 strict-Claude quota 대기. 통합/복구 담당은 `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`. 이들은 서로 다른 역할이며 검증 완료가 아니다. | 기존 strict-Claude 작업을 보존하고 해당 OpenCode 검증 요구의 결과를 기록. 중복 worker 생성 금지. |
| Offline 좀비 Run / lease 복구 | 사용자 원문(회사 scope desk): “이상하게 죽거나 멈춰서 좀비가 된애들 정상으로 만들어주는 그런 보상처리좀해주라”. 이는 이후 식별된 개인 recovery Run을 회사 scope로 실행하라는 뜻은 아님. 별도 broad COVERAGE 요청은 중단된 배포·권한 작업을 전체 복구하라는 취지. | 대상 Run `2ea0d05a-bbe6-4afe-b9c0-90a88951a411`; originating session `f61dbaa4-adc0-460d-bd41-1652153d8ee4`; recovery contact `b99551fe-e4a2-498e-b4d5-e26421e91e04` | 대상 offline lease 회수/Run 재개는 과거 work-history에서 미실행. b995는 기존 recovery contact이며 실행 완료 증거가 아니다. 살아 있는 GAME Run 취소/회수 금지. | 새 운영 gate 통과 후 지정 owner가 신선한 ownership/fence를 확인해 기존 Run만 처리. 이 handoff에서는 dispatch/reconcile하지 않는다. |

## root 운영 메모 (오케스트레이터 인계, 사용자 원문 아님; 2026-10-30)

> 출처 정정: 이 체크포인트 묶음은 사용자 직접 발화가 아니라 오케스트레이터 인계로 전달됐다. 아래 root 상태 요약은 인계된 운영 보고이며, 사용자 원문으로 인용하지 않는다.

- `48eac0c6` 배포는 succeeded/active를 직접 확인.
- 복구 dispatch 실호출 `LOG-297`은 없는 `GenerationRuntime.draining` 때문에 HTTP 500 실패. fake PASS는 운영 보장이 아님.
- Copilot 새 AIP 실 Run `LOG-296`은 `str.items` 오류로 실패.
- 두 실패 교정의 단일 owner는 `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`.
- 이전 AIP `33885923…`은 cancelled, 신규 `a1eccb27…`만 유효. 최신 AIP/회사 레그나/GAME Studio 사용자 scope는 유지.

## 판정 경계

- 과거 전수완료 주장은 **FAILED 유지**. 미복원된 원 100개와 선정·제외 기준은 이 작업으로 회복되지 않았다.
- 네 요구의 출처를 승인 저장소/지원 대화에서 연결했으며, 사용자 원문이 없는 감사자 요약은 문서 근거로 표시했다. 보호된 다른 Run outbox는 읽지 않았다.
- 수행한 변경은 이 새 문서 추가뿐이다. 테스트, worker 생성/취소, 코드·main·배포 변경, 운영 dispatch/reconcile은 하지 않았다.
