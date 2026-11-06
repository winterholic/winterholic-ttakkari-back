# 작업 기록: REGNA 마찰 분석 대조 및 연구 런타임 결함 수정 (2026-11-06)

## 1. 개요
- **목표**: `docs/research-lab/regna-friction-and-improvements.md` 분석 결과에 기반하여, 개별 런 실패 격리, 에러 원인 분류 정정, 재개 시 configurations 보존, LLM 프롬프트 및 응답 궤적 영속화를 전용 worktree에서 구현 및 검증.
- **워크트리**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-lab-practical-20261106`
- **브랜치**: `fix/lab-practical-20261106`

## 2. 변경 파일 및 요약
1. `src/ttakkari/research/runner.py`:
   - `execute_run_instance`: 어댑터가 실패한 경우(`status == "failed"`) 원래 `error_code`/`error_message`를 보존하며 타임아웃으로 인한 `budget_exceeded` 오분류를 방지. 일반 예외 발생 시 `isinstance(e, TimeoutError)`만 타임아웃 취소로 처리하고 나머지는 `failed`로 분류.
   - `_run_owned_batch`: 배치 중단 사유를 전체 예산, 사용자 취소, 메모리 가드로 명확화하고, 개별 런 실패 시 후속 설정을 격리하여 계속 실행.
2. `src/ttakkari/research/service.py`:
   - `snapshot`: 연구 내 실험들의 `Configuration` 레코드들을 조회하여 `configurations` 배열과 `totals["configurations"]`를 반환하도록 보완.
3. `src/ttakkari/host_gpu.py`:
   - `bench`: 반환 딕셔너리에 `prompt` 전문 및 `runs[i]["content"]` 전체 응답 텍스트를 보존.
4. `src/ttakkari/research/adapters/llm_inference.py`:
   - `execute_run`: `raw_output`에 `prompt` 및 `sample_trajectories`를 구조화하여 영속화.
5. `unit_tests/test_research_regna_improvements.py`:
   - 5건의 단위 테스트 추가 (스냅샷 설정 보존, 프롬프트/궤적 보존, 실패 에러코드 보존, 예외 분류, 배치 격리).

## 3. 검증
- `uv run pytest unit_tests/test_research_regna_improvements.py` 5건 통과
- `uv run pytest unit_tests/test_research_terminal_and_dispatch.py` 3건 통과
- 총 74건 연구 관련 단위 테스트 전체 통과
