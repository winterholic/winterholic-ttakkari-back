# 2026-10-16 Antigravity 모델 우선 배정

사용자는 기존 Flash-only·Antigravity Claude 제외 결정을 바꾸고, 포함 quota를 활용해 Claude를 우선 배정하며 Flash와 Argon도 적극 사용하도록 요청했다. 진행 중인 연결/MCP 수명 수정과 분리해 모델 선택·계약·한도 처리를 변경했다.

| 작업 | 우선 모델 |
|---|---|
| 일반 구현·조사·글쓰기·UI | Antigravity Claude Sonnet 5.5 high |
| 단순 검색·추출·변환·작은 수정·정해진 검사 | Gemini 3.8 Flash high |
| 어려운 설계·추론·독립 대안 | Gemini 4 Argon high 또는 Antigravity Opus 5.5 medium/high, 구체적 사유 필요 |
| 해당 quota 소진·필요 capability 미검증 | 적합한 다른 quota 그룹 또는 native Claude/Codex |

- `agy models` 1.3.6에서 실제 조회한 13개 모델을 서버 계약에 추가했다. 버전이 붙은 Antigravity Claude ID는 native Claude alias와 구분한다. 모델 ID의 생각 수준과 effort 불일치를 거부하고, 생략 시 ID 수준을 적용한다. Antigravity 기본값은 Sonnet high다.
- 최신 정책을 역할 지침 뒤에 주입해 예전 Luna-first·Flash-only·Claude 제외 지침이 남아 있어도 사용자 최신 결정을 적용한다. 메인 오케스트레이터의 native Opus medium/GPT Sol high는 유지한다. native auto의 계정 분산과 회사 범위도 유지한다.
- 선택 모델의 Claude/GPT·Gemini·별도 Argon quota를 독립 판단한다. 런타임 quota 거절도 그룹별 안정 UUID로 기존 cooldown 저장 계약에 보존하고, 계정 한도 초기화 시 하위 그룹도 해제한다. 정확한 잔여율을 사용하므로 0.1%를 반올림해 소진 처리하지 않는다. 빠진 Argon 창은 unknown으로 두며 다른 그룹의 잔여량을 복사하지 않는다.
- AI 크레딧 0을 baseline quota 0으로 해석하지 않는다. 유료 overage 설정·구매는 변경하지 않았다. [공식 모델](https://www.antigravity.google/docs/models), [baseline과 overages](https://www.antigravity.google/docs/plans)를 확인했다.
- 별도 진행 중인 Luna backend benchmark 커밋 `10a4a1e`를 보존해 통합했다. 통합 검사에서 새 read-only 도구 `luna_common30_v2_status`의 기대 목록 누락을 보완했다. Antigravity MCP 설정 삭제 시점 코드는 이번 변경에서 수정하지 않았다.

검증:

- 실제 CLI: `claude-sonnet-5-5-high`, `gemini-4-argon-high` 각각 무작위 표식 응답 SUCCESS 및 요청한 모델 ID 일치. 명령 sandbox의 단발 응답 증거이며 운영 MCP·재개 검증은 아니다.
- 실제 Sonnet 배정 평가: 기존 충돌 지침을 앞에 둔 8개 사례에서 정상 구현·UI·단순 추출·어려운 설계·그룹별 소진·크레딧0·MCP 결함을 기대대로 구분(8/8). 단일 모델·단일 평가의 결과다.
- 집중 통합: API의 실제 fake CLI 인자 확인, 기본 모델·생각 수준, 승격 사유, 그룹별 차단·재로딩·초기화, 정책 주입 및 read-only 계약 28개 통과.
- 전체 검사 초기 실행은 분리 checkout의 `.venv/bin/alembic` 부재로 중단됐다. 검증 중에만 기존 venv를 연결해 준비하고 migration 이후를 이어 실행했다. Luna 조회 도구 기대 목록 보완 후 남은 구간도 재개했다. 원본 실패 XML은 보존했다.
- JUnit을 (classname, name)의 최신 실행으로 합치고 현재 collect 결과와 대조: 930개 중 928 통과, 기존 xfail 2개, 미실행·최종 실패 0개. Ruff와 diff check 통과. 단일 전체 명령이 한 번에 통과했다는 주장은 하지 않는다.

원시 증거: `/Users/winterholic/.ttakkari/data/outbox/4d331c5f-324f-42e5-a352-df31e3b90c81/`의 모델별 smoke JSON, `live-routing-policy-eval.json`, `routing-results.xml`, 각 구간 회귀 XML, `combined-regression-results.xml`, `regression-coverage.json`.

운영 배포는 clean 통합 SHA를 독립 generations 감독에 전달한다. 접수·전환·구세대 배출을 구분하고, 운영 실제 Run·인증 MCP·재개의 성공은 별도 증거가 있어야 한다.
