# AI Research Lab 초기 구현 검증 기록

2026-10-20 KST. 검증 대상은 이번에 구현한 초기 경로이며 요구사항 전체의 모든 확장 기능이 아니다. 대상 환경과 검증 표면을 섞지 않는다.

## 로컬 코드·통합·마이그레이션

명령은 `back-research-lab` / `front-research-lab` worktree에서 실행했다. 백엔드는 `PYTHONPATH=src`와 기존 개발 venv를 사용한다. DB 테스트는 `tests/conftest.py`의 프로세스별 임시 PostgreSQL DB에서 수행했다. 운영 DB에 테스트를 실행하지 않았다.

```sh
env PYTHONPATH=src ../back/.venv/bin/pytest \
  tests/test_generation_deployment.py tests/test_kb_migration.py tests/test_kb_library.py \
  tests/test_research_lab.py tests/test_agent_pms.py tests/test_knowledge.py \
  unit_tests/test_research_rules.py unit_tests/test_research_benchmark.py -q
```

관측: `101 passed in 12.59s`. 원본: Run outbox `backend-research-tests.log`.

포함한 경계: 인증, 정확하지 않은 인용, 실행을 가장한 관찰 입력, 회사/개인 범위, 다른 연구 참조, 낙관적 version 충돌, 알려지지 않은 개념/관계, 조건 미상/불일치, 수치 예산, 잘못된 qrels, 직접 측정 receipt, 완료 실험 중복 실행 거부, 새 세션 토큰의 복원, KB 원본 bytes 보존, 연구 이력 DB 불변성, 같은 범위 과거 방법의 재사용.

```sh
npm run lint
node_modules/.bin/vitest run src/pages/ResearchPage.test.tsx src/shell/AppShell.test.tsx src/api/client.test.ts
npm run build
```

관측: lint 통과(기존 UI baseline 138건 이하, 새 증가 0), 29 tests passed, TypeScript 및 Vite production build 완료. 원본은 `frontend-research-{lint,tests,build}.log`. 번들 크기 경고는 기존 큰 chunk에 대한 경고이며 빌드 실패가 아니다. 운영 프론트의 배포 완료나 실제 원격 API 연결을 이 결과로 주장하지 않는다.

Alembic은 임시 새 DB에서 `upgrade head`를 실제 수행했다. 새로운 head는 `0056_research_lab`, 선행은 KB의 `0055_kb_immutability_hardening`이다. SQLAlchemy metadata와 새 research 5개 표/인덱스/제약을 대조한 결과 차이 0. 정적 migration의 FK는 `kb_evidence`를 참조한다. 전체 `alembic check`에는 기존 ctx/meeting embedding/lexical 인덱스와 weather unique 표현 등 다른 도메인의 차이가 출력되므로 전체 정합성 검사가 깨끗하다고 말하지 않는다. Research 표에 한정한 실행 비교만 통과했다. outbox: `research-migration-parity.log`.

generation 계약에는 이전 허용 이력을 보존하고 0051~0055를 online predecessor로 추가했다. 신규 Lab 표·함수·트리거는 기존 서비스 데이터를 바꾸지 않는 추가형이다. 기존 0052 보관함 마이그레이션 등 선행 작업의 동작은 upstream 그대로 포함된다. ready/ownership/drain을 우회하지 않는다.

## 실제 라이브 테스트와 브라우저

테스트 서버는 `tests/research_smoke/live.py`, 주소는 `127.0.0.1:18791`, 별도 프론트 Vite는 `18792`이다. 합성 테스트 계정만 사용한다. `tests/research_smoke/scenarios.py`가 API를 통해 실제 사례 자료와 실험을 만들고 snapshot을 outbox에 저장한다. 이것은 운영에 생성된 연구가 아니다.

공식 `ttakkari_browser` MCP의 headless isolated 브라우저에서 로그인 → 새 연구 작성 → 공유 query로 다시 진입 → 근거/실험/계획 저장·복원 → 실험 두 개 선택/조건 확인을 실행했다. 데스크톱 1280 폭과 모바일 390 폭을 확인하고 스크린샷·DOM 박스를 outbox/browser에 보관했다. Lab 영역 폭은 각각 664 / 358px에서 본문이 해당 영역에 배치됐다. Safari 실기기나 다른 엔진의 검증은 아니다.

브라우저 개발 환경에는 기존 ServiceWorker 등록 MIME(`text/html`) 오류와 첫 로그인 전 refresh 401이 있었다. 로그인 후 연구 생성·조회·저장·비교는 동작했다. 또한 개발 Vite가 파일 변경 후 이전 변환 모듈을 응답하는 현상을 직접 curl로 확인했다. 개발 서버를 다시 시작한 뒤 `served_section=true, served_compare=true`인 최신 모듈을 확인하고 새 비교 UI를 다시 조작했다. 이는 이 개발 서버의 관측이며 운영 캐시 원인을 판정한 것이 아니다.

사례 D에서 두 논문은 모두 원문 보고로 표시됐고 조건부 관계를 함께 읽을 수 있었다. 사례 A의 두 외부 보고를 비교하자 누락된 모델 리비전/하드웨어/소프트웨어 등으로 공정 비교 판정을 거절하는 화면을 확인했다. 조건을 모른 채 효과 개선을 보증하지 않았다.

## 실제 사례 A~E

- A: `host_gpu_start/status/bench/stop`를 사용한 호스트 GPU 경로 측정. Qwen3.5-4B Q4_K_M, context 4096, threads 4, FA on, 동일 22 prompt tokens / 96 generated tokens, 각 3회. 중앙 생성 속도 f16 121.7337 / q8 122.4972 tok/s. 각 구간 swap growth 0, 종료 `running=false, stop_reason=requested`. GPU layer 로그는 빈 배열이라 정확한 장치 배치는 미검증. 품질 동등성·35B·긴 문맥·통계적 개선은 미검증. 도구 응답은 Lab에서 `external_report`로 저장해 직접 실행과 구분했다.
- B: 최신 KB가 반영된 실제 ADR 54개 / 질문 3개 / k=5. lexical과 BM25의 Recall@5 및 MRR가 모두 0.6667. 개선 미확인. 질의별 순위·원본 config·해시·환경을 저장했다. 운영 검색 서비스의 품질 향상을 채택하거나 유의성·생성 품질을 측정한 것이 아니다. 이전 53개 파일럿과 혼동하지 않는다.
- C: 공개 Zero-shot CoT 연구의 정확한 발췌와 저자 보고를 Finding으로 저장하고, 작동 원리 미확정은 별도 Hypothesis로 보존했다. 원 논문을 재현하지 않았다.
- D: RAG/LC 연구와 OP-RAG 연구의 서로 다른 결론을 Claim 및 조건 비교 필요 관계로 저장했다. 모델/데이터셋/자원 조건 전체를 초록에 없는 값으로 채우지 않았다.
- E: 기존 요약을 제공하지 않은 새 GPT-6 Luna/Codex 세션이 라이브 resume API를 실제 호출했다. 목표, paused, 성공 실험 1건, 54개 문서·질문 3개·같은 두 방식 결과·다음 작업을 복원했다. 그 시점에는 실패 실험이 없었음을 정확히 보고했다. 원본은 `research-resume-independent.md`. 이후 의도적 negative control을 실제 실행하여 `state=failed,error=invalid_qrels`를 저장했고, 추가 실패와 다음 행동을 새 컨텍스트에서 복원하는 독립 후속 검증을 요청했다. 이 추가 결과는 별도 독립 보고서가 정본이다.

E의 독립 세션이 발견한 study dataset revision과 receipt config hash의 차이도 대조했다. 같은 저장 config를 ASCII escape / UTF-8 JSON으로 직렬화한 두 지문과 각각 일치함을 실제 계산해 확인했다. `research-resume-with-failure.json`의 `hash_mapping`에 근거를 보존한다. 해시가 다르다는 이유만으로 데이터를 달리 측정했다고 단정하지 않았고, 후속 smoke script에서는 UTF-8 직렬화를 사용한다.

## MCP·배포의 별도 경계

MCP 함수의 호출 계약은 `_call` spy를 통해 POST `/api/agent/research`, operation/study_id 전달을 확인했다(`wire contract passed`). 세션 API의 실제 인증·범위는 HTTP 통합 테스트에서 확인했다. **새 코드가 배포된 뒤 엔진에서 `manage_research`를 실제 호출한 검증은 별개이며 아직 이 문서의 증거에 포함되지 않는다.**

배포는 깨끗한 커밋과 push 후 기존 `request_backend_deploy`에 full SHA를 전달한다. 요청 수락은 배포 성공이 아니다. outbox의 `deploy-reservation.json`이 제출 당시 상태의 정본이며 실제 활성화/이전 세대 drain 완료는 컨트롤러 결과로 구분한다. 진행 중 Run 때문에 배포를 임의 보류하거나 Run에서 재시작 스크립트를 실행하지 않는다.

## 남은 제품 범위

자동 주기 수집·전체 논문 파싱·여러 제공자 수집 adapter, calibrated 정보 가치·조합 탐색, 장시간 GPU/학습의 자동 큐/중단/체크포인트, OS 수준 메모리 예산, 일반 artifact 모델/데이터셋 통합, 연구 규모별 pagination, 임베딩·리랭킹 및 통계 검증, 더 다양한 시각화는 후속 범위다. 초기 전체 경로의 실행 검증과 이 확장 작업의 완료를 섞지 않는다. PMS에 별도 확장 작업으로 남긴다.
