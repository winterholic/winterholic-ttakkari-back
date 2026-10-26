# 요구별 구성 인수와 남은 운영 인수

2026-10-26, backend 제품 `eb3bcf6f49638b9f76d269d5da9f79d76966545e`,
현재 evidence HEAD `76bbcbacff1c66a6a2a5a91cb6a331b2b1ec4e80`, frontend `5a2650fde8d5c46f50c9f08ce67f14917f14da98`.
원문 R1~R16의 문구를 기준으로 이미 실행·인수된 범위를 대조했다.
배포 대기 중 같은 모듈 검사를 다시 실행하지 않았다. 아래 완료는 해당 요구의 구성 인수이며 전체 제품 완료가 아니다.

## 유효 증거와 현재 소스 연결

이번 실제 명령:

```sh
git diff --exit-code e9e3260 -- src/ttakkari/research src/ttakkari/api/research.py src/ttakkari/artifacts src/ttakkari/tts src/ttakkari/host_gpu.py alembic deploy/generation-contract.json
```

출력 없음, exit 0. 현재 위 제품 경로는 기존 통합 인수의 e9e3260과 같다.
후속 dispatch 교정은 `src/ttakkari/runs/manager.py`이며 별도 관련 회귀/contract 검증과 controller 전체 gate를 유지한다.
프론트 `git status --short --branch`는 clean, `git rev-parse HEAD`는 위 5a2650f다.
두 worktree 및 이전 인수 문서의 명령·범위를 대조한 결과이며 이번 새 전체 회귀 결과가 아니다.

## 구성별 인수

| 요구 | 판정 | 유효한 실행 근거와 한계 |
|---|---|---|
| R1 다분야·다계층 Registry | done | 코어 17e0f84, backend-review-0c37878.md의 다차원 검색·분류/정의·런타임 경계. 112개 seed의 6축과 확장 구조를 인수. 출처 전수 진위나 실제 Metal 지원을 뜻하지 않는다. |
| R5 선택·조합 설계 | done | backend-review-0c37878.md의 충돌/선후/조건·fail-closed와 후속 stage2 교정; acceptance-coverage-e9e3260.md/combination-cpu-e9e3260.json의 실제 CPU 2방법 2×2 grid4/repeat2; deployment-recovery-ui-237f135.md의 실제 조합 UI·설정·차트·reload. 개별 방법의 개선을 조합 개선으로 추정하지 않았다. |
| R6 파라미터 폼 UI | done | c-reuse-contract-fix.md 및 final-ui-adoption-review-5a2650f.md의 실제 UI k5 무수정 재사용/k7 명시 변경·실행·reload. 서버 저장값·metric과 대조했고 source/default/adapter/노트 payload 교정이 포함된다. |
| R8 Definition/Run·반복 저장·외부 결과 | done | 코어/API 인수, remaining-integration-review-a564c05.md의 실제 API/PG 출처 충돌18반례·정상 freshread·manual/import; 실제 CPU grid4/repeat2의 설정·repeat_index·raw·compare fresh GET. 예산 제한·미지원 탐색은 기존 planner 계약에 따르며 모든 탐색 알고리즘의 네이티브 구현을 주장하지 않는다. |
| R9 성과·실패·설정 차이 시각화 | done | final-ui-adoption-review의 기록/설정/해석·reload, lead-final-ui-integration-564d497.md의 동조건 CPU A/B delta0 UI, deployment-recovery-ui-237f135.md의 조합4조건 표/차트·실제 KeyError와 연구자 해석/전체reload. 반복 분포/Pareto 데이터와 프론트 계약의 유효 인수를 재사용. bounded 데이터의 관측이며 일반 성능 향상·자동 원인 설명을 뜻하지 않는다. |
| R10 목표별 최선 결과·아티팩트 | done | final-ui-adoption-review의 목표/metric/제약/설정/Run 채택·reload; lead-final-ui-integration-564d497.md의 실제 CPU 리포트 생성→파일 보관→reload→content/download, 662bytes와 SHA256 일치. runner-registration-atomicity-review-9f087f0.md의 등록 예외·취소 보존도 인수. |
| R11 과거 연구·방법 재사용 | done | c-reuse-review-763dc03-e1158f0.md/API15의 4source FK·scope/ID/원본불변·실패설정 재사용; final-ui-adoption-review의 실제 UI k5 복제/k7 변경·실행·출처/reload. source-refresh-eb3bcf6.md의 출처 새 버전과 인용/조건 보존도 보완한다. 자동 수집 플랫폼 연계까지 확대하지 않는다. |

R2는 기존 구성 인수 done을 유지한다. 위 신규 done 7개를 합해 8개가 구성 인수됐지만 완료율로 해석하지 않는다.

## 남은 요구와 실행 소유자

- R3: 새 방법/가설·외부 candidate 보존은 기존 범위만 인수. 최종 D 흐름·운영 대조가 남는다.
- R4/R7/R12: 새 활성 runtime의 실제 A Metal/RuntimeLease/raw, 운영 지원 표시 및 원문 UX10/A~D 최종 대조.
- R13: 기존 운영 연구 eb41e3a7의 필드별 baseline과 활성 후 비교. 전체 DB row/lock 및 legacy 92b3c8e7 full ID는 미검증이다.
- R14/R15: controller 전체 회귀와 backend 활성, frontend main/Vercel, 운영 smoke.
- R16: PMS 6/7/8의 원래 완료 조건 최종 대조. 7.x의 자동 수집/선택 반례/장기 복구/500초과 탐색/확대 평가는 위 bounded 인수로 완료 처리하지 않는다.

이번 한 번의 배포 조회는 b8bb518b testing/skip_tests=false/error=null.
지원 ops summary는 37줄/2,785 bytes, 약45% 진행/sections=[]였다. 오류 없음은 최종 PASS가 아니다.
실행 소유자는 기존 controller이며 중복 예약·대기 폴링·제품 변경은 없다.
전체 목표는 active다. 새 요구나 기존 완료 기준 축소는 없다.
