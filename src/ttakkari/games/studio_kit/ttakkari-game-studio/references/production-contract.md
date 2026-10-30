# 제작 완료 계약

`studio gate` 가 판정하는 규칙이다. 정본은 `lib/ledger.mjs` 의 `POLICY` 이고 이 문서는 설명이다.

## 범주별 필요한 증거

| 범주 | 기계 증거(any-of) | 사용자 확인 | 왜 |
|---|---|---|---|
| bug | `play-replay` 또는 `reach-check` | 불필요 | 진행불가·입력·충돌은 실제로 실행해야 안다. 단위 테스트 통과는 플레이 가능을 뜻하지 않는다 |
| logic | `machine-test` | 불필요 | 데이터·저장·계산 |
| balance | `machine-test` 또는 `play-replay` | 필요 | 봇이 깨도 재미는 보증 못 한다 |
| art | `screenshot` | 필요 | 렌더 화면을 보고, 취향은 사용자가 |
| ux | `screenshot` 또는 `play-replay` | 필요 | 화면·조작 흐름 |
| narrative | `canon-check` | 필요 | 정전 위반은 기계가, 서사 재미는 사용자가 |
| feel | `play-replay` 또는 `screenshot` | 필요 | 타격감·연출 |
| audio | `play-replay` | 필요 | 헤드리스는 소리를 못 듣는다 |

## 판정

- `FAIL`: 열린 항목, 요건 증거 부족, 증거 파일 변조·소실(sha256 불일치), 정전 위반, 에셋 오류, 린트 오류.
- `NEEDS_HUMAN`: 기계가 확인할 것은 끝났고 주관 항목의 사용자 확인 또는 `not-run` 이 남았다. 원장이 비어 있어도 여기까지만.
- `COMPLETE`: 모든 항목 검증 + 주관 항목에 `user-confirmed`(원문 quote + source).

`play-replay` 만 있고 `repro-before` 가 없으면 "재현이 버그를 실제로 잡는지 증명되지 않음"이 미검증으로 남는다.

## 증거 유형

| type | 붙이는 법 | 도구가 막는 것 |
|---|---|---|
| play-replay | `play --attach ID` (통과) | ok=false 결과 |
| repro-before | `play --attach ID --phase before` (실패) | ok=true 결과(재현이 버그를 못 잡음) |
| reach-check | `reach --spec s.json --out f.json` 후 `ledger evidence --type reach-check --ref f.json` | ok=false 결과, 도구가 안 낸 JSON |
| canon-check | `canon --json > f.json` 저장 후 attach | 위반 있는 결과 |
| screenshot | 실제 렌더 캡처 파일 | 이미지가 아닌 파일, 단색·투명 PNG |
| machine-test | 로그 파일 + `--note "exit 0"` | (logic·balance 에서만 요건으로 인정) |
| user-confirmed | `--quote 사용자 원문 --source 세션·메시지 위치` | quote·source 없음 |
| not-run | `--reason` | 사유 없음. 요건을 채우지 못하고 미검증으로 보고된다 |

## 한계 (도구가 증명하지 못하는 것)

- 재미, 화풍 일관성, 서사 품질, 소리, 모바일 실기기 터치. 이것들은 사람이 본다.
- `reach` 명세가 실제 게임 충돌체와 같은지. 명세 추출기는 게임마다 쓰고 실플레이로 교차 확인한다.
- 린트는 정규식 단서다.
