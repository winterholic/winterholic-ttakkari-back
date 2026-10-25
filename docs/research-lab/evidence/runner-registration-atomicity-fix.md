# Runner 파일 등록 중 실패 교정 — 2026-10-25

대상 parent: `f1dfaaa66eeb97267fc7235de110ae3805375f29`.
이전 독립 검증: `runner-evidence-review-121ab9e.md`.

## 재현과 원인

Codex 명령 환경의 표준 주입 PostgreSQL에서 identity를 먼저 확인했다.
`select current_database(), current_user` → `ttakkari_test / ttk_test`.
운영 DB·기존 실패한 PG 복원 경로는 사용하지 않았다.

파일 선언 사전 검사는 입력 누락을 잡지만, 이후 등록 자체의 실패는 막지 못했다.
runner는 측정값과 Artifact/ResearchArtifact를 한 트랜잭션에 넣었다.
`register_file`은 그 트랜잭션을 commit하기 전에 UUID 디렉터리에 사본을 만든다.
두 번째 등록 후 예외가 나면 DB rollback은 측정값도 지우지만 사본은 지우지 않았다.
취소 처리에는 rollback이 없어 일부 연결을 commit했다.

등록 후 예외, 부분 복사 후 OSError, 실제 PostgreSQL FK flush 오류, 취소를 주입한
새 회귀를 제품 수정 전에 실행했다:

```text
.venv/bin/python -m pytest -q tests/test_scenario_b_artifacts.py -k registration_failure
4 failed, 7 deselected in 0.65s
```

앞 세 경로는 Measurement 0건, 취소 경로는 부분 ResearchArtifact 연결로 실패했다.
반례 자체는 정상 파일·실제 파일 보관 서비스·실제 DB를 사용하고 오류 지점만 주입한다.

## 교정

- 파일 등록 전에 raw/env/측정을 commit하되 Run은 `running`으로 유지한다.
  모든 파일과 연결이 commit된 뒤에만 `succeeded`로 바꾼다.
- `register_file(created_copies=...)`은 새 UUID 디렉터리를 만들자마자, 복사 전에
  목록에 기록한다. 반환 전에 실패해도 호출자가 정확한 사본 위치를 안다.
- 등록/DB commit 오류와 취소 시 파일 등록 트랜잭션을 rollback하고 그 목록만 정리한다.
  원본 파일·기존 보관 파일·legacy ID는 삭제 대상으로 넣지 않는다.
- 정리 실패는 로그와 Run error_message에 남긴다. 삭제 실패를 조용히 무시하지 않는다.
- 롤백될 ID가 adapter raw에 들어가지 않도록 artifact 선언을 변이하지 않는다.

## 검증

원래 반례 4건에 artifact 최종 commit 시 실제 FK 오류도 추가했다.
새 SQLAlchemy session에서 terminal 상태·raw/env·RTF 측정 1건,
신규 Artifact/ResearchArtifact 0건, 저장 디렉터리 증가 0건을 대조한다.
기존 marker와 원본 두 파일 보존도 확인한다. 정상 WAV API·dedupe 기존 회귀는 유지한다.

```text
.venv/bin/python -m pytest -q tests/test_scenario_b_artifacts.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_v2_api_contract.py tests/test_research_adapters.py tests/test_research_v2_catalog.py tests/test_research_v2_migration.py tests/test_research_lab.py tests/test_stage2_defects_fix.py tests/test_stage2_b_norm_contrast.py tests/test_artifact_area.py tests/test_broker_policy.py
313 passed in 13.74s
```

변경 3개 Python 파일의 Ruff 진단을 parent와 비교한 새 진단은 0건이다.
기존 Ruff 진단 7건은 남아 있으며 전체 lint 통과 주장은 아니다.
최초 넓은 테스트 명령은 존재하지 않는 `tests/test_broker.py`를 잘못 지정해
`no tests ran`이었다. 위 명령은 실제 `test_broker_policy.py`로 정정한 실행이다.

이 검증은 예외 처리 경로의 파일/DB 정합성 범위다. 갑작스러운 프로세스/호스트 종료의
분산 원자성을 보장하지 않으며, 실제 TTS 재측정·브라우저·A Metal·배포는 실행하지 않았다.
이전 실제 Supertonic/Whisper 증거는 재실행하지 않고 해당 revision의 범위로 보존한다.
새 독립 검증을 거치기 전 최종 인수로 표시하지 않는다.
