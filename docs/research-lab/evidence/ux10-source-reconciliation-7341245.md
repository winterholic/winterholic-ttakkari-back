# 원문 UX 10문·D 구성 인수의 현재 소스 대조

2026-10-26. backend `0d042d50d32993f359c0d9b10049ce9c8e4f45cc`,
frontend `7341245fe230a14728ea61fa5075748827cf8ed1`.
원문 §5의 질문 순서를 유지하고 기존의 서로 다른 인수 범위를 합쳤다.
새 UI/실험 실행이나 운영 인수 보고서가 아니다.

## 이번 직접 확인

`python3 docs/research-lab/evidence/reconcile-ui-evidence-7341245.py` exit0.
보존 JSON에서 D의 후보/미검증/어댑터 필요/실측0, 별도 연구의 같은 method 재사용,
새 가설의 hypothesis_stage, 승격 decision, 별도 DB 행 및 raw/env/metric 출처 일치를 확인했다.
후보를 validated로 조작한 임시 JSON은 `external result remains candidate`로 거절했고,
파일이 없으면 FileNotFoundError로 중단했다. 원본 증거와 제품은 수정하지 않았다.
원시 결과와 13개 증거 파일 해시는 [JSON](ux10-source-reconciliation-7341245.json)에 있다.
해시는 보존 파일의 동일성을 식별하며 그 내용의 진위나 새 실행을 보증하지 않는다.

backend의 연구/API/artifact/TTS/host_gpu/migration/generation-contract는 e9e3260과 diff0다.
frontend는 5a2650f 이후 D 교정의 5파일만 바뀌었고 현재 HEAD가 D 영수증의 revision과 같다.
따라서 기존 C·조합·실패·아티팩트 인수는 범위 안에서 재사용한다.
R3의 등록→실행 지원 안내→외부 보고→후보→카탈로그 후속 재사용은 구성 인수 done으로 판단한다.
외부 결과는 검증됨으로 승격하지 않았고 합성 1tok/s를 실측이나 개선으로 판단하지 않았다.
R12의 운영 D 경로 및 전체 제품 인수는 별도로 남는다.

## 원문 질문과 증거

| 원문 질문 | 기존 유효 근거·후속 교정 | 남은 범위 |
|---|---|---|
| 1. 시도 가능한 방법? | [remaining-integration §5](remaining-integration-review-a564c05.md)의 라이브러리 검색/필터·분야/계층 설명. [D](scenario-d-ui-contract-fix.md)의 새 가설 등록·목표/계층/지표 폼과 후속 연구 선택 | 운영 화면. 옛 리뷰의 다른 Run 스크린샷 원본은 이번에 열지 않았으며 당시 리뷰의 관찰 범위만 인용 |
| 2. 현재 실행 가능한 방법? | 기존 fail-closed 인수 + D의 등록 직후 hypothesis_stage와 candidate/adapter_required/실측0. mutation 뒤 서버 getMethod를 읽는 교정 포함 | 새 활성 runtime의 실제 Metal 및 운영 지원 표시 |
| 3. 이전 시도? | [C UI](final-ui-adoption-review-5a2650f.md)의 원본/후속/성공·실패 reload, [실제 실패](deployment-recovery-ui-237f135.md)의 KeyError와 측정0 | 운영 스모크. C의 초기 synthetic 실패와 실제 입력 실패를 구분 |
| 4. 적용 설정? | C UI의 k5 원본 충실 복제·k7 명시 변경/실제 cutoff. 조합 영수증의 4개 설정과 원문 대응 | 운영 화면 |
| 5. 조합? | [CPU 조합](acceptance-coverage-e9e3260.md)의 두 방법 grid4/repeat2, [후속 UI §UX5](deployment-recovery-ui-237f135.md)의 실제 4설정·차트·전체 reload | 운영 화면. 임의 조합의 효과를 일반화하지 않음 |
| 6. 개선/악화 폭? | [리드 UI](lead-final-ui-integration-564d497.md)의 동조건 A/B delta0 및 조합의 비교 API/UI | 운영 화면. k5/k7은 다른 cutoff이므로 개선으로 비교하지 않음 |
| 7. 성공 이유·실패 원인? | [후속 UI §UX7](deployment-recovery-ui-237f135.md)의 실제 입력 KeyError 원문/연결 Run과 연구자 해석(unverified) 저장·reload·fresh resume. 이번 보존 JSON에서 노트 동일성/측정0 확인 | 운영 화면. 자동 인과 판정이나 품질 실패라고 주장하지 않음 |
| 8. 재실행 가능한 실험? | [C API](c-reuse-review-763dc03-e1158f0.md)의 실패 Run rerun과 repeat2 저장 계약 | 운영 MCP 지속 연구 resume; 장기 owner crash/drain은 PMS7.3의 별도 조건 |
| 9. 현재 최고 결과물? | C UI의 목표/지표/제약별 채택·reload, [리드 파일 UI](lead-final-ui-integration-564d497.md)의 실제 report 생성/보관/열기/다운로드. 이번 보존 JSON에서 662bytes/content200/download hash 일치 확인 | 운영 화면. 모든 목표의 보편적 최고나 음성 파일을 새로 검증한 것이 아님 |
| 10. 이전 연구 재사용 방법? | C의 source4 연결·원본 불변·k5 복제/k7 변경·실행/reload. D의 별도 연구 same method 선택과 출처 보고 저장·reload | 운영 스모크. 자동 collector/선택 calibration은 PMS7.1/7.2 별도 조건 |

기존 UX5/7/9의 격리 인수 공백은 후속 실행으로 보완됐다. 이 대조만으로 UX10 운영 PASS를 선언하지 않는다.
앞선 리뷰의 역사적 FAIL/PARTIAL은 삭제하지 않고 위 후속 근거와 연결했다.

## 배포·PMS 경계

controller `02141811-90f9-4145-afc2-17e1ee98de01` / revision `12aaf56`을 이번 Run에 1회 조회했다:
`testing / skip_tests=false / error=null`. ops summary 46줄/3484bytes, 57%/sections0.
이는 전체 gate 통과나 활성 증거가 아니다. frontend fresh fetch는 origin/main 대비 behind0/ahead19,
origin/main 조상 exit0. main push/Vercel 배포는 아직 실행하지 않았다.

R4/7/12는 실제 A Metal/lease/raw와 운영 UX가 남고 R13은 baseline 운영 필드 대조,
R14/15는 controller 전체 gate·활성·Vercel·운영 smoke가 남는다.
R16/PMS 6·7·8은 원래 조건을 유지한다. PMS7.1 자동 재수집/추출, 7.2 선택 편향/상충/조합 반례,
7.3 장기 복구, 7.4 500초과 탐색/관계 그래프/실제 payload 계측, 7.5 확대 acceptance는 미완이다.
bounded CPU/합성/단기 UI 인수를 이유로 이를 done 처리하지 않았다.

다음 실행 소유자는 기존 controller다. 활성 후 version→frontend/Vercel→실제 A→운영 MCP/UX 및
PMS 미완 조건 실행을 이어간다. 중복 backend 예약·대기 폴링·유효 suite 재실행은 하지 않았다.
