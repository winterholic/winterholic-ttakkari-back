# 2026-10-16 작업자 배정 정책을 코드로 통합

사용자는 네이티브 Codex/Claude 쏠림과 Antigravity 저활용 진단 후 꼼꼼한 개선·검증을 요청했다. 원인은 worker auto의 Antigravity 제외, 도구 안내와 최신 역할 정책의 충돌, 모델/작업 성격보다 계정 여유를 먼저 비교하는 기본 경로였다. 메인·접수는 사용자 기존 결정대로 native 정책을 유지한다.

## 변경 계약

- `runs/worker_routing.py`를 단일 정책 원본으로 두고 신규 auto 작업자에 적용한다. general=Antigravity Sonnet→Flash→native Sonnet/Luna, routine=Flash→Luna→Antigravity Sonnet→native Sonnet, design=Antigravity Sonnet→native Sonnet, verification=Luna→Antigravity Sonnet→Flash→native Sonnet, reasoning=Argon→Antigravity Opus→native Sol/Opus다. 같은 단계의 계정은 기존 Run 당 여유로 분산한다. reasoning 및 peer 모델은 사유를 요구하며 native peer effort도 기존 Sol high/Opus medium으로 맞춘다. 이 표는 사용자 작업 선호를 실행하는 정책이며 보편적인 모델 성능 순위나 새 품질 벤치마크가 아니다.
- `task_type`이 없으면 general이다(peer 요청은 reasoning으로 분류해 사유를 요구). 예전 Luna 모델 선호를 검증 작업으로 간주해 Codex에 고정하지 않는다. 지정 모델이 작업 정책에 없으면 조용히 무시하지 않고 400으로 명시 엔진 선택을 안내한다. 명시 엔진은 사용자/필요 도구에 따른 실제 잠금으로 보존한다.
- fresh 모델그룹 창의 정확한 remaining_percent를 headroom에 반영한다. Claude/GPT·Flash·Argon pool, zero credits, stale 소진, 초기화 후/미측정 unknown을 구분한다. unknown보다 알려진 적합 후보, 그 안에서 모델 단계와 계정 부하를 비교한다. 회사 범위·켜짐·지원 모델·cooldown을 지킨다.
- `account_usage.worker_routing`, `model_capacity`, 역할 지침의 단계 목록이 같은 원본에서 생성된다. 최신 정책이 낡은 역할 지침 뒤에 붙고 MCP 설명도 일치한다. `worker.routing` 이벤트와 WorkerOut.routing에 선택·실제 모델·quota group/창·측정시각·잔여·점수·제외·트리거를 기록한다.
- 모델별 provider 대화를 `account_id:model`로 분리한다. Sonnet→Flash→Sonnet 재개 시 같은 계정이라도 Flash의 진행을 `_handoff_context`로 전달한다. 모델을 바꿔 같은 실행파일 부재를 반복하지 않으며 일반 실패/권한 거부를 quota처럼 다른 엔진에서 재실행하지 않는다.
- `message_worker(reroute=True, task_type=...)`는 idle 작업자를 새 auto 정책으로 전환하고 기존 대화를 보존한다. running/queued이면 409로 거부하고 새 지시·context reset도 적용하지 않는다. legacy Run이 요청 모델명을 그대로 보존한 native fallback 이력도 counterpart를 복원해 맞는 provider 대화를 재개한다. 부작용 확인/완료·취소 이후 전환하며 완료 작업을 자동 재실행하지 않는다.
- 0035는 sessions.worker_task_type nullable 컬럼만 추가한다. 기존 세션/진행 중 Run을 자동 변경하지 않는다. generation protocol/소유권/슬롯/drain 조건을 유지하고 온라인 호환 계약은 0035로 갱신한다. 별도 회의·날씨 라우팅은 변경하지 않는다.

## 검증 및 증거 범위

증거 디렉터리: `/Users/winterholic/.ttakkari/data/outbox/3f5bf6da-ce10-4829-be72-e36a013c259a/`.

- RED: 기본 auto의 native 선택, AGY headroom 미반영, reasoning 사유/잘못된 task_type, 모델 preference 무시, native peer effort, 같은 계정 모델 간 진행 손실, 실행파일 중복 시도, legacy counterpart resume 누락을 각각 재현하고 수정했다. `*-red.txt` 원시 결과를 보존했다.
- task·모델별 소진·런타임 quota·all-capped wait·회사 범위·unknown/stale/reset·지원 모델·명시 잠금·idle/active reroute·일반 실패·모델별 재개/진행 인계와 실제 generation ownership 경로를 synthetic CLI/격리 DB에서 검사했다.
- 초기 전체 검사: 1095 passed, 3 skipped, 2 xfailed, 5 failed. 4개는 분리 checkout의 `.venv/bin/alembic` 경로 누락으로 테스트 환경을 연결했다. 1개는 native auto 재개 시 어느 계정이 먼저 풀리는지에 따라 Claude/Codex 둘 다 가능한데 Claude만 가정한 검사였다. 실제 선택 엔진과 맞는 argv를 읽고 동일 Run·진행/체크포인트·단일 알림·resume 이벤트를 검사하도록 보완했다. 보완 뒤 관련 76개 통과. 원본 실패 로그는 `full-regression.txt`에 남긴다.
- migration-check.txt: 별도 DB에 0034를 실제 설치하고 구 배포 ORM으로 running legacy Run/대화 ID를 쓴 뒤 online 0035 upgrade를 실행했다. status/engine/대화 ID 보존 및 nullable task_type을 SQL로 확인했고, 구 ORM에서도 동일 데이터를 조회했다. 운영 DB에 migration을 직접 실행하지 않았다.
- routing-production-snapshot.json: 저장된 운영 quota/cooldown/실행 lease를 읽기 전용 트랜잭션에서 재생해 개인 general/design=AGY Sonnet, routine=Flash, verification=Luna, reasoning=Argon 선택을 확인했다. 회사 범위는 개인 계정을 빌리지 않는다. 이 파일은 모델 호출·운영 새 라우터 실행·MCP·CLI 재개의 증거가 아니다.
- GPU 모니터링 main 변경을 통합한 최종 전체 회귀 결과와 커밋/배포 상태는 아래에 기록한다.

최종 검증: GPU main 통합 코드 `4da9b32e1448b4d51bc21ceb915ba2509f6dca8e`에서 단일 전체 실행 `python -m pytest -q --tb=short` → **1118 passed, 3 skipped, 2 xfailed, 6 subtests passed (222.04s)**. `final-full-regression.xml/txt`가 원시 증거다. Ruff 전체 및 diff check도 통과했다. 최종 문서 기록 이후에는 실행 코드를 바꾸지 않았다.

배포 상태: 이 기록 시점에는 **미적용**이다. canonical back에는 다른 세션의 Qwen 구현·0035_qwen_token_usage 변경이 미커밋으로 남아 있어 변경을 섞거나 stash하지 않았다. 이 브랜치는 GPU main을 포함한 별도 clean commit으로 보존한다. 예약 시도의 원시 응답은 outbox/deployment-response.json에 남긴다. 이후 Qwen 변경과 합칠 때 양쪽의 0035 migration이 단일 그래프가 되도록 정리하고 schema 계약/마이그레이션/관련 회귀를 다시 확인해야 한다. 운영 새 라우터의 모델 응답·MCP·CLI 재개는 아직 미검증이다.
