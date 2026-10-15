# 2026-10-16 세대별 무중단 배포 개발

- 사용자 요청: 설계 후 개발도 진행. 운영 배포/서비스 전환은 이번 요청에서 실행하지 않음.
- 작업 위치: `/Users/winterholic/development/projects/winterholic-ttakkari/back-generation-deploy`, 브랜치 `feat/generation-deploy-20261016`.
- 코드 커밋: `f7069f65ff81e162a192d9c4b39172290ca90ab2` (`Add opt-in generation-based backend deployment`). 원래 운영 checkout/main은 변경하지 않음.
- 구현: opt-in standby/active/draining 세대, DB CAS·Run 소유권·fencing, 전역 lane/세션/계정 예약, 제어 명령, DB 이벤트 깨우기·SSE cursor 재개, singleton STT/preview/vectors/주기 작업, immutable git archive·전용 venv·브라우저 MCP Node 런타임, launchd 슬롯 제어 코드, Caddy 설정 전환·rollback·감독 재조정, 후속 예약 대기·동일 계약 누적 커밋 병합, 요청 멱등 키, 0030 additive migration.
- 새 앱이 남의 Run을 전역 recovery로 중단하지 않도록 legacy recovery도 보호했다. active 슬롯 재기동 시 신규 요청 담당자만 새 UUID로 갱신하며 이전 소유 Run을 추정 재실행하지 않는다.
- 테스트에서 추가로 잡은 경계: 실패한 claim이 남의 Run을 failed로 바꾸는 오류, stale 예약의 재실행, 소유권을 잃은 desk retry의 DB 쓰기, archive에서 `.git` 없이 테스트할 때의 revision 주입, 기존 browser MCP 설치 누락.
- 최종 전체: 격리 DB에서 `python -m pytest -q -x` → **800 passed, 2 xfailed, 6 subtests passed in 152.73s**. `ruff check src tests alembic`와 `git diff --check` 통과.
- 고정 릴리스: Git archive에 실제 venv와 Node 의존성을 설치하고, 별도 DB의 실제 schema에서 `GenerationController.prepare`를 실행한 뒤 archive 안에서 신규 기능/두 ASGI/Caddy/SSE/버전 테스트 **33 passed in 8.43s**. 실제 서비스 제어는 호출하지 않음.
- migration: 독립 DB upgrade → downgrade(0029) → upgrade와 runs/runtime_* 모델 비교 통과. 전체 autogenerate 비교의 기존 raw vector/lexical 인덱스 11개 차이는 baseline과 새 코드에 동일함을 별도 DB에서 확인. 이 차이를 삭제하거나 전체 check 통과로 보고하지 않음.
- 운영 한계: 기본 runtime_generation=false, deploy_strategy=idle. 최초 launchd/Caddy bootstrap·운영 활성화·실제 LLM/STT·장시간/고부하·CPU/RSS 검증은 미실행. 초기 서비스 설치 자동화 전체와 release GC는 미구현이며 운영 전환 문서에 명시함. 현재 코드 개발 범위와 운영 완료를 구분함.
- 운영 전환 문서: `docs/design/generation-deployment.md`. 결과/원시 로그: `/Users/winterholic/.ttakkari/data/outbox/2c2fdbca-1454-4e16-a897-ad2ef7df480e/`.
- 후속 운영 적용 시: 검증된 별도 브랜치 변경을 통합한 뒤 기존 외부 배포 예약 규약을 지킨다. 최초 전환은 장기 작업이 모두 끝난 안전 구간에 외부 설치 작업으로 수행하고, 감독 tools 릴리스·source checkout·슬롯 릴리스를 분리한다. Run 안에서 launchctl/재시작을 실행하지 않는다.
