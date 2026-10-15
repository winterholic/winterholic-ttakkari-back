# Qwen 3.5-9B shadow observer 준비·활성화·복구

## 현재 상태

- 운영 Luna/Haiku의 prompt, 후보 선택, 실행, 응답은 그대로 유지한다.
- `qwen35_shadow_enabled` 기본값은 `false`, `qwen35_shadow_sample_rate` 기본값은 `0.0`, 상한은 5%다. 앱 시작 시 모델·queue consumer를 만들지 않는다.
- 비운영 설정을 명시적으로 켜면 현재 desk가 만든 request/context, ADR 0013 및 실제 desk 지침 해시, `personal`/`work` 범위와 그 시점의 lead/model/tool/permission 후보를 복사해 process-local bounded queue에 넣는다. desk는 도구·권한 후보가 없으므로 빈 목록이다.
- 입력은 allowlist 기반 기록 객체로만 구성한다. 일반 식별자·비밀·연락처·URL·경로·코드 패턴을 redaction하고 남은 패턴은 fail-closed한다. `redaction_counts`는 request/context별 변경 종류와 개수만 보존한다. 실제 라벨은 `policyverified/manualpending`, `regression_candidate=false`로 시작한다. 기존 Luna/Haiku 결과는 자동 정답이 아니다.
- queue는 최대 4개, 60초 TTL이며 full/expiry/shutdown/validation 실패는 내용 없는 카운터로만 남긴다. isolated inference는 20초 timeout, 16 KiB response cap, reply 3,000자 schema cap을 쓴다. 현재 route에는 queue consumer·Qwen 실행·영속 sink가 연결되어 있지 않다. 따라서 현재 변경만 배포해도 live collection/paired evaluation은 수행되지 않는다.
- 명시적으로 호출하는 미래 sink는 `personal`과 `work` 디렉터리를 분리하고 디렉터리 0700, 파일 0600, atomic write, 7일 보존을 적용한다. 현재 어떤 실제 경로에도 쓰지 않는다.

## staging opt-in 수용 절차 (추후 별도 승인 후)

1. 코드 review와 사용자 승인, 배포 승인, staging 전용 계정·workspace 범위 확인을 먼저 끝낸다. production config/env 파일은 수정하지 않는다.
2. staging에서만 앱 설정으로 `qwen35_shadow_enabled=true`, `qwen35_shadow_sample_rate=0.01`을 적용한다. 우선 1%를 쓰고, 사용자가 승인한 뒤에도 5%를 넘기지 않는다. production 환경에서는 설정이 참이어도 observer가 거부한다.
3. 지금은 수집 consumer와 관측 endpoint가 없으므로 실제 sample을 모으거나 평가를 시작하지 않는다. 별도 후속 구현이 consumer, 제한된 로컬 sink 경로, 카운터 노출을 연결하고 별도 review를 통과해야 한다. 실행 전 route input과 candidate snapshot, scoped 0700/0600 저장, 만료 정리, timeout·output cap·drop 경로를 합성 fixture로 검증한다.
4. 후속 수집이 승인된 경우, request/context redaction 결과와 변경 범위를 확인한다. 개인 scope는 personal 경로, 회사 scope는 work 경로에만 기록한다. 원문, feedback note, account/session/run 식별자는 별도 필드로 저장하지 않는다. 실제 membership 검증에 필요한 현재 lead 후보 ID만 후보 snapshot 안에 둔다. 기존 baseline 판정은 저장하지 않으며 샘플 라벨을 자동 정답으로 바꾸지 않는다.
5. paired replay는 해당 시점에 실제 저장된 동일 request/context/policy/candidate snapshot만 대상으로 한다. 같은 snapshot을 Qwen과 Luna/Haiku에 전달하고, 정책 교정은 사람이 승인한 표본만 분모에 둔다. 합성 결과는 별도 분모다.

## 관측과 중단

- 시작 전: enabled/env/sample-rate, staging scope, 고정 policy digest, model/tool/permission/lead candidate set을 기록한다.
- 실행 중: enqueue/drop/expiry/shutdown/error counters, queue depth/capacity, inference timeout, schema·candidate validation 실패, output-size 초과를 별도로 센다. 실패는 live route에 전달하지 않는다.
- 즉시 중단 기준: 민감 패턴 scan 실패, personal/work scope 혼선, 후보 snapshot 누락·변조, silent sample loss, queue 상한 위반, route 응답/실행 차이. 하나라도 발생하면 enabled=false로 돌리고 수집물을 격리한 뒤 원인 검토 전 재개하지 않는다.
- 재개 기준: 원인 수정, 합성 negative/positive fixture 통과, staging에서 live route 응답·후보·도구 실행이 기존과 같다는 review가 필요하다.

## 복구

1. 승인된 staging 설정에서 enabled=false 및 sample rate=0으로 되돌린다. 필요하면 통상 배포 절차로 staging 프로세스를 교체한다. 이 run에서는 설정 변경·배포·재시작을 실행하지 않았다.
2. process-local queue를 close해 잔여 sample을 drop하고 shutdown_drop 카운터를 확인한다. 수집이 승인되어 저장 파일이 생긴 뒤라면 해당 scope 디렉터리를 격리하고 승인된 보존·삭제 절차를 따른다. personal/work 데이터를 합쳐 복구·복사하지 않는다.
3. 안정화 후에도 Luna/Haiku route를 그대로 둔다. 재평가는 정책 교정 데이터와 같은-input paired replay, 별도 성능/자원 평가가 준비될 때까지 보류한다.

## 미완료 경계

현재 변경은 준비 hook, 메모리 queue, 명시적 private sink 함수와 runbook까지다. live consumer·sink 활성화, 설정 변경, 운영/배포 통합, live paired 평가, 정책 정정 라벨 확정, Qwen 모델 실행은 아직 하지 않았다.
