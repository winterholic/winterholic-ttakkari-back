# 2026-10-16 재배포 통합

- 사용자 요청: 어제 작업 후 배포 여부를 확인하고 미배포면 재배포.
- 확인: 운영 `/api/version`은 d61e3a0ad7e493c176c398f3030cf3156b4d4535, PID 61369. 마지막 예약 c9a554da-b13e-42c8-979e-f71422bb6225는 예약 후 작업 트리 변경으로 failed.
- 기존 변경은 `git stash`의 `safety: preserve all pre-deploy changes 2026-10-16`에 원형을 보존했고 삭제하지 않았다. 원격 main의 사용량 표시 4개 커밋과 로컬 observer 통합 테스트 커밋을 merge했다. 기존 sequencer 메타데이터는 `/Users/winterholic/testing/ttakkari-deploy-preservation-20261016/original-sequencer/`에 보존한 뒤 `git cherry-pick --quit`으로 정리했다.
- 미커밋 동시 실행 상한 16/16/16, 사용량 상한의 stale 차단, 실행 수를 반영한 계정 분산 및 관련 문서·테스트를 통합했다. 실험 자료도 보존한다.
- 오래된 작업 트리에 남은 Antigravity 오류 처리·cleanup·검증 설명 삭제는 통합된 HEAD의 안전장치를 유지하는 방향으로 정리했다. Antigravity quota/credits 수집을 설명하도록 기획 지침의 미관측 단정도 바로잡았다. Qwen 실험 활성화나 운영 모델 교체는 하지 않았다.
- 새 격리 DB에서 pytest 관련 7개 파일: **102 passed in 22.64s**. `ruff check src tests`: All checks passed. `alembic heads`: 0029_antigravity_usage_credits 단일 head. `git diff --check HEAD` 통과. 테스트 DB는 종료 뒤 삭제했다.
- 독립 배포 감독이 새 SHA로 전체 검사와 실행 종료 대기 후 migration/restart/health/version 검증을 수행하도록 예약한다. 이 기록은 배포 성공을 뜻하지 않는다.
