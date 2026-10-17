# 리버스 엔지니어링 모델 합성 단계 (2026-10-18)

독립 검증 failed 판정(정적 휴리스틱·테스트 2건 실패 등)에 대한 수정 세션.
ADR 0040, 백엔드 `src/ttakkari/reverse.py` 중심, 프론트 폴링 수정은
격리 워크트리 front-reverse에서 origin/main에 반영(9e295e8).

## 백엔드 변경(back)

- `src/ttakkari/reverse.py`
  - 단계 6→8개: survey(모델 조사)·synthesize(모델 합성) 추가. 정적 단계는 사전 조사로 유지.
  - 모델 경로는 `worker_routing` availability(reasoning/personal) 1순위,
    없으면 ROUTES reasoning의 desk 실행 가능(codex/claude) 폴백. 실행은 `desk._ask`.
    모델은 상대경로+인라인 발췌만 본다. 테스트 교체 지점 `MODEL_REVIEW`.
  - 합성 검증: 근거 없는 주장 버림(버린 건수 한계 명시). 파일명 키워드 템플릿 `_ideas` 삭제.
  - summarize 단계 evidence 누락 수정(결함 2-1).
  - 대상 검증: 로컬은 `validate_workspace_root`(allowed_roots), 원격은 https+허용 호스트(기본 github.com),
    loopback·사설·링크 로컬·localhost·비공개 IP 거부.
  - clone: followRedirects=false·credential.helper 비움·file 프로토콜 차단. git 타임아웃은
    프로세스 그룹 kill+wait. clone 512MiB·reverse 루트 2GiB 상한(끝난 작업부터 정리).
  - 걷기: os.walk 그 자리 가지치기+WALK_CAP 정지. SKIP_DIRS에 .venv/site-packages/uv-cache/.stt-runtime 등.
    진입점 후보에서 tests/test/examples/website 제외. 심링크 걷기·topdirs 제외.
  - 재시작 복구 `recover_reverse_jobs` + `main.py` lifespan 연결. 목록 API에도 stale 적용.
    종료 전이는 SELECT FOR UPDATE 조건부 전이(FAILED↔DONE 덮기 방지).
- `src/ttakkari/config.py`: `reverse_allowed_hosts` 추가(기본 빈 목록=github.com만).
- `src/ttakkari/main.py`: lifespan에 리버스 복구 연결.
- `alembic/versions/0043_reverse_jobs_jsonb.py`: stages/result JSON→JSONB. 0041 미수정.
  origin에 0042_artcraft·ADR 0039가 먼저 올라와 번호를 0043/0040으로 변경.
- `deploy/generation-contract.json`: schema_revision 0043, online_from에 0042 추가.
- `docs/adr/0040-reverse-model-synthesis.md`: 설계 변경 기록.
- `tests/test_reverse.py`: `_wait_done` prefix 인자(에이전트 경로 수정, 결함 2-2),
  8단계 기대, 가짜 모델 종단·폴백·검증 단위·stale 단위 테스트 7건 추가(총 12건).
- `unit_tests/test_reverse_static.py`: DB 없이 도는 순수 테스트 11건(샌드박스 실행용).

## 프론트 변경(front, 9e295e8)

- `ReversePage.tsx`·`ReverseDetailPage.tsx`: 폴링을 refetchInterval 함수형으로 바꿔
  최신 데이터 기준으로 active 계산(결함 6). 2쿼리 합성 제거.
- `reverseTypes.ts`+상세: `stats.model`(엔진/모델/발췌 수) 표시.
- `ReversePage.test.tsx`: 8단계·모델 표시 포함 5건.

## 검증

- `ruff check` 변경 파일 전체 통과.
- `pytest unit_tests/test_reverse_static.py`: 11 passed.
- `pytest unit_tests`(전체): 263 passed, 3 failed — muse 어댑터/TUI 3건은 깨끗한 트리에서도
  실패하는 기존 결함(확인함). 내 변경과 무관.
- `pytest tests/test_reverse.py`: 12건 전원 ERROR(세션 픽스처에서 Postgres 접속 PermissionError).
  이 샌드박스가 loopback TCP·유닉스 소켓 connect를 막아(EPERM) 스크래치 DB 기동 불가.
  이전 세션처럼 운영 파이프라인에서 돌려야 한다. 미실행.
- `tsc -b` 통과, `eslint` 통과, `vitest run ReversePage.test.tsx` 5 passed, `vite build` 성공.
- rea 실측(rev ceeff5a4, shallow 76M): 정적 4270파일·hub 8·체인 2 → 모델 survey 24파일 지정,
  synth dropped 0, 모듈 12·흐름 4·아이디어 7·한계 12. 근거 줄 교차 확인함
  (scripts/rea.mjs:14-16 MCP 분기, src/composition/binary.ts:48-70 provider 등록).
  codex CLI는 샌드박스에서 뜨지 않아 실측은 ROUTES reasoning의 claude 후보(opus)로 수행.
  운영(계정 등록)에서는 availability 1순위를 쓴다. 결과 `/tmp/rea-result.json`,
  사용자 요약은 outbox `reverse-rea-2026-10-18.md`.

## 배포

- 백엔드 main 병합·푸시 후 `request_backend_deploy` 전체 SHA 예약(예약 ID 아래).
- 프론트는 main 직접 반영(기존 방식): 9e295e8.
- 주의: canonical `front/` 체크아웃에 다른 세션의 미커밋 변경·미푸시 분기가 있다. 건드리지 않았고,
  front 작업은 격리 워크트리에서 했다. `front-reverse` 워크트리는 남겨둔다.
