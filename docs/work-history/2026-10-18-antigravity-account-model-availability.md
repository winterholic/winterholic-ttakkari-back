# 2026-10-18 Antigravity 계정별 모델 가용성

증상: 계정 "내 antigravity"가 `claude-sonnet-5-5-high`를 받아 즉시 `invalid model selection`으로 실패했고, `reroute=True`로도 같은 계정·모델이 다시 선택됐다.

원인: `worker_routing`은 정적 단계표와 `model_catalog`(한 계정에서 관측한 ID)만 보고, 계정별 모델 목록과 CLI 거절 결과를 후보 계산에 쓰지 않았다. 거절 실패는 한도·실행 파일 없음과 달리 폴백 조건이 아니어서 Run이 그대로 끝났고, 쿼터가 가장 넉넉한 같은 쌍이 다음 배정에도 뽑혔다.

변경:
- `runs/agy_models.py`: 계정 홈에서 `agy models`를 실행해 계정별 목록을 6시간 캐시한다. 목록을 모르면 제외하지 않고(fail open), 아는 목록에 없거나 CLI가 거절한 (계정, 모델)만 제외한다. 거절은 6시간 기억하며 이후 목록에 포함되면 해제한다.
- `profiles.availability`가 제외 대상을 후보에서 뺀다. `manager`는 `invalid model selection`을 감지하면 해당 쌍을 기억하고 `account.model_unsupported`와 `engine.fallback(reason=invalid_model)`을 남긴 뒤 다음 후보로 넘어간다.
- routing receipt에 `excluded: [{account, model, reason, detail?}]`를 넣는다. reason은 `not_in_account_model_list` 또는 `cli_rejected_model`.
- 설정 `antigravity_model_probe`(기본 true)로 조회를 끈다. 테스트는 끈다.

한계: 다른 계정 ID로의 매핑은 하지 않는다. 해당 계정의 CLI가 ID가 아닌 표시 이름만 오류에 보여 주기 때문이다. 제외된 계정은 목록에 있는 다른 모델(예: Flash)로만 배정된다. 목록·거절 기억은 프로세스 메모리라 재시작하면 다시 조회한다. 고정 엔진(pinned) 세션에서 요청 모델이 그 엔진 계정 전부에서 제외되면 폴백 없이 "켜진 계정 없음"으로 끝난다.
