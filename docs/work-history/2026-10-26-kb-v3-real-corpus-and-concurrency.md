# KB v3 실제 기존 자료·E5·MCP 및 동시 처리 교정

전체 목표는 active다. 원요구 PDF 자동 심층 추출과 상충 주장 실제 여정, 최신 main 통합·배포·운영 백필은 남았다.

## 실제로 실행한 범위

- 리드 Run `5787f6de-caf0-4d23-b1ed-681a2b59b1d9`, Codex sandbox. 백엔드 시작 HEAD `6d158ba4f9c36a029f5099cf7d6dda05dc6de7f8` (src는 `3a6d69a`와 같음). Front 재시험은 안 했다.
- 현재 Run이 제공한 loopback DB `ttakkari_test`/`ttk_test`와 Run marker의 run_id·port가 일치함을 확인했다. 운영 DB·공유 DB·다른 Run outbox는 읽지 않았다.
- 지원 MCP `kb_documents`/`kb_document`로 개인 세션에 보이는 기존 운영 KB 36건 전체를 내보냈다. 문서/리비전/패시지 ID와 본문·해시·출처 메타데이터가 있다. 원본 바이트, 실제 접근 행, 완전한 관측 원장은 내보내지 않았다.
- 사본: `/Users/winterholic/development/testing-assets/kb-v3-corpus-20261026.json`. 저작물 본문 전체를 git에 복제하지 않고 결과에는 corpus SHA-256을 남겼다. 내보낸 내용의 참/거짓이나 원문 사이트의 현재성을 승인한 것이 아니다.
- 빈 전용 DB를 0071로 만든 뒤 그 API 사본의 기존 ID·본문·해시를 넣고 0073으로 올렸다. 36건 모두 문서/리비전/패시지 ID·본문 해시 보존. 접근 행은 사본 시험을 위해 research 21건 personal, Kubernetes 15건 work로 만들었으며 운영 접근 행을 복제했다고 주장하지 않는다.
- SDK 실제 stdio MCP → HTTP → PostgreSQL 왕복에서 회사 15/개인 21건 목록·상세·근거를 읽었다. ID/해시 36건 일치, 개인에서 회사 문서/근거 guessed-ID 요청 2건 404. 이 코드는 MCP legacy 분할을 보존하므로 회사에서 personal 자료까지 36건을 볼 것이라는 첫 checker 가정을 수정했다. SDK input_schema/is_error 명명도 설치본에 맞게 고쳤다. 최초 실패 로그도 보존했다.

## E5 제품 검색 비교

- 모델: 기존 설정의 `intfloat/multilingual-e5-large`, fastembed 공급원 `Qdrant/multilingual-e5-large-onnx` revision `ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b`. 작업 공간 cache에만 다운로드했다. 모델 카드: https://huggingface.co/Qdrant/multilingual-e5-large-onnx . 모델·서버를 운영 보호 경로에 설치하지 않았다.
- 고정 revision snapshot_download는 main ref를 만들지 않아 fastembed local_files_only 첫 조회가 실패했다. 작업 cache refs/main을 이미 받은 고정 SHA로 지정한 뒤 실제 제품 `kb_embedding_worker`가 2×1024, full coverage 벡터를 반환했다. 런타임 worker는 계속 offline이다.
- 실제 사본 36건에 기본 백필 dry-run→apply: enqueue_only 21, rechunk_same_body 15, 오류 0. 원본 HTML은 사본에 없으므로 헤딩을 복구했다고 말하지 않는다. 현재 235 passage 벡터가 실제 E5 model_key로 색인됐다.
- 순위를 보기 전에 질문·qrels를 고정했다. 35개 문서 찾기 진단 질문, 한→한 6/한→영 29, 원문을 읽은 에이전트의 양성 근거만 있다. 사용자 검토·사용자 질의 holdout·완전한 음성 정답 집합은 없다. 기존 kb_eval.metrics를 문서 ID 단위로 재사용하며 미판정 hit는 음성으로 간주하지 않는다.
- 실제 HTTP 105요청, 오류 0. lexical Recall@5 0.5714 / MRR@10 0.5160, vector 0.8857 / 0.7550, hybrid 0.8571 / 0.7494. p50/p95 초: lexical 0.0090/0.0917, vector 0.9222/1.0042, hybrid 0.9711/1.0672.
- 표본에서는 hybrid가 vector보다 낮다. 이 표본에 맞춰 순위를 조정하지 않았다. 지연은 요청별 모델 자식 기동을 포함한 1회 관측이며 처리량·통계 유의성을 주장하지 않는다. L2는 꺼져 있어 그래프 커버리지는 L0/L1 기준이다. 전체 품질 승인이나 의미적 근거 정확도 판정이 아니다.

## 실제 사본으로 드러난 동시성 결함과 교정

- 자동 백필 중 여러 문서가 동일 identity를 SELECT한 뒤 동시에 INSERT하여 `uq_kb_entities_norm` 중복 키가 났다. 다른 순서로 두 엔터티를 만드는 쓰기도 deadlock을 일으켰다. 기존 자동 재시도 후 36 job은 모두 degraded로 끝났지만 불필요한 재시도가 있었다.
- 실 PostgreSQL 4개 동시 transaction에서 시드/일반 해소 생성 경합, 두 transaction의 역순 생성 3건을 새 회귀로 고정했다. 코드 변경 전 3 FAIL: 앞의 2건 UniqueViolationError, 마지막 deadlock detected. `identity-red-actual.log`.
- identity resolver 및 직접 seed 보장 진입점에서 ontology tenant 단위 PostgreSQL transaction advisory lock을 잡는다. 여러 visibility가 global identity를 같이 참조할 수 있으므로 visibility로 잠금을 쪼개지 않는다. commit/rollback 시 해제되며 모델 계산은 잠금 밖이다. SQLite in-memory 단일 쓰기 시험에는 PostgreSQL 잠금을 실행하지 않는다.
- 새 3회귀 GREEN. 관련 실제 PG/API/관계/추출 및 SQLite 해소 묶음: `84 passed, 1 skipped in 13.19s`. skip은 별도 KB3_PG_URL schema-reset fixture이며 새 동시성 3회귀는 실제 PG에서 실행됐다. 첫 묶음은 SQLite에서 PG SQL을 실행해 24 failed/60 passed/1 skipped였고 원시 실패 로그도 보존했다. ruff 및 git diff --check 통과.
- 교정 커밋 `ecdea4b6dc240c9b688b96f0243a5131f024b7a1`을 실제 서버에서 실행했다. 기존 자료 본문 36건을 4건씩 동시에 등록하고 자동 worker가 처리했다. 36/36 attempt=1, 서버 로그에서 UniqueViolation/deadlock/실패 traceback 0건. parsing/chunking/linking/indexing/canonicalizing은 성공, extracting만 `l2_disabled`이므로 전체는 36건 모두 degraded다. 이 결과를 자동 심층 추출 성공으로 세지 않는다. `replay-batch.json`, `replay-batch.log`, `server-fixed.log`.
- 검색 측정은 교정 전 SHA이며 검색 소스는 이번 교정에서 바꾸지 않았다. 교정 후 같은 순위 묶음을 불필요하게 반복하지 않았다.

## 미검증과 환경 경계

- 실제 tool-free subscription transport를 codex/gpt-6-luna/high로 1회 호출했으나 `nonzero_exit` (약 19초)로 끝났다. 원시 provider 오류는 transport가 고정 진단으로 감싸므로 이번 호출의 401 여부는 확인되지 않았다. 코드상 `_env`가 상속 CODEX_HOME을 제거하고 프로필 config_dir=None은 기본 로그인을 쓰는 것은 확인했다(원인 추론). 알려진 이전 기본 로그인 401과 이번 결과를 같은 실행 증거로 합치지 않는다. 관리 계정/비밀 파일을 직접 읽거나 복사하지 않았다. account_usage상 native Claude 계정은 모두 disabled라 호출하지 않았다.
- 새 L2 실행 가용성·PDF/충돌 E2E는 미검증이다. hash 검색이나 수동 assertion 삽입으로 대신하지 않는다.
- 전용 서버 `/api/version`이 반환한 본인 PID SIGTERM은 Codex command sandbox에서 Operation not permitted로 거부됐다. 다른 shell 우회는 하지 않았다. 지원 exec 세션 `write_stdin` Ctrl-C로 exit 130을 얻었고 이어 loopback connection refused로 실제 종료를 확인했다. 이후에만 같은 DB pytest를 실행했다.
- main 작업 폴더의 무관한 다수 변경은 그대로 보존했다. 통합은 별도 clean worktree에서 해야 한다. main/push/deploy/PMS done은 아직 하지 않았다.
- 최신 `origin/main`을 fetch한 결과 `77c1238a2b8ade01c096dc74b4f4da3a52c89849`다. 이쪽 schema head는 `0073_research_reuse_source`이고 KB head는 `0073_kb_l2_budget_ledger`이므로 후속 통합에서 Alembic merge revision 및 generation contract 양쪽 경로를 보존해야 한다. 아직 병합하지 않았다.

## 재현·증거

`review/kb-v3/final-gaps-20261026/`의 environment.py, prepare_legacy.py, mcp_roundtrip.py, product_search_eval.py, qrels.json, run_identity_regressions.py와 각 로그/JSON을 사용한다. 실제 source corpus와 E5 cache는 위 작업 공간 경로를 필요로 한다. 각 준비 도구는 현재 Run 소유 marker를 확인하며 prepare_legacy는 비어 있지 않은 DB를 거부한다.
