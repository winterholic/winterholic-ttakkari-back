# 2026-10-16 지식 기반 재설계와 첫 구현

## 사용자 결정

전체 지식 누락률 미측정은 구조 개선을 보류할 이유가 아니다. 원본 보존·관계 발견/누락·교차 프로젝트·개인 맥락·시간/정정 요구를 종합해 설계와 WBS를 갱신하고 바로 구현 착수하라는 명시 지시를 받았다. 과거 '기존 유지/감사 승인 대기'를 대체한다. 원본 영구삭제·민감 추론 자동확정·scope 확대는 포함하지 않는다.

## 정본과 WBS

- docs/knowledge/architecture.md: 요구→설계→인수 추적표, 목표 모델·API·단계/실패/롤백·평가 계약.
- ADR 0031; ADR 0009·README는 현 운영과 구현 예정 구조를 구분해 교차 링크.
- PMS project e99a75de-7ccf-48e6-a5d1-46e455eb79b8 / 실행 parent 6d82e98c-c8f7-4993-a027-e12430c71b15 (5.4.6).
- 기존 6개 task 재사용 + KD/K6/K7/K8 4개 추가 = 10개. 외부 승인 gate 의존 제거, 선행관계 명시.
- 5.4.4는 이번 지시 반영으로 done, 과거 설계 문서 작성/감사는 역사적 사실을 보존하고 새 정본으로 연결.
- KD(a85ec3e-9cd8-4fcc-9f96-499c8674792f) 설계 작성 done. K1(8b908a58-8a50-4140-a2e4-9372e8609e1a) doing: 원본 parser 첫 tranche 구현, 전체 lineage schema까지 완료한 것은 아니다.

## 첫 구현

context-inventory CLI: 모든 ChatGPT shard/node/message/branch occurrence의 안정키·source SHA/JSON pointer·내용 hash·발화자·처리 사유. 문서 제목·원문 텍스트·asset locator를 출력하지 않음. 미지원 modality와 null node도 원장에 남김. 중복/충돌·깨진 JSON·parent/cycle·no-input·symlink·상한을 명시. DB/모델 호출 없음. 출력은 원본 밖 새 private bundle, 기존 결과 덮어쓰기 금지. 새 CLI parser는 현재 검색/색인에 자동 연결하지 않는다.

## 실행 증거와 한계

- RED: 신규 원본 원장 기능이 없어 Source inventory feature is missing으로 실패를 관찰한 뒤 구현.
- 초기 unit 22 passed. 새 deadline 음성 대조 실패를 확인한 뒤 POSIX standalone CLI timer 추가; 최종 unit 25 passed.
- 실제 export root 존재 확인 후 두 probe가 os.scandir 디렉터리 열거에서 머물러 중단(KeyboardInterrupt/exit130). 명령 실행 환경의 현상이며 운영 파일 접근 실패로 일반화하지 않음. 수정한 CLI --timeout3은 exit2 / counts={} / inventory_deadline_exceeded로 종료. 원본 census·실제 지식 보존율은 미검증.
- 합성 fixture CLI: 1 conversation/3 nodes/2 messages; no_message/pending_extraction/unsupported_content 각각1. 원본 실측 결과가 아니다.
- 첫 전체 회귀: 전용 ttakkari_knowledge_test_20261016 DB에서 964 passed/2 failed/2 xfailed/6 subtests. 두 실패는 격리 worktree의 .venv/bin/alembic 부재. 기존 가상환경 symlink로 테스트 실행 경로를 맞추고 전체 재실행. 최종 결과는 아래에 추가.
- Ruff changed Python files: All checks passed. git diff --check 통과.
- 설계 HTML은 configured ttakkari_browser에서 390x844/1280x900 확인, 최종 console errors0. 실제 앱의 새 지식 UI는 아직 구현 전.
- 현재 API /api/version 응답 revision 110cbe857f21bac2849b76d0ed7d4ffb6f0b9964, /api/health {ok:true}. 로컬 deploy/current 파일의 9d0b6fa 값과 달랐다. current 파일만으로 활성 revision을 판단하지 않는다. /api/ready는 HTTPError로 응답했으며 이번에는 status/body 미수집. 이 사실을 generation 기능 부재나 장애로 판정하지 않는다. 운영 DB와 감사 로컬 snapshot 동일성은 미검증.

산출물: outbox/253bbc75-ed96-4373-a259-dfceaea22f3b의 설계 HTML·MD, WBS JSON/CSV, synthetic-inventory-example, chatgpt-source-inventory(unknown timeout 원장), pytest 로그, browser screenshots.

## 남은 작업

K0 runtime/DB identity와 corpus 기준선, K1 후속 immutable schema·adapter·기존 원본↔문서/기억/관계 연결, K2 assertion/history, K3 추출, K4 검색, K5 정정, K6 독립 평가, K7 모바일 가시화, K8 전수 staging/shadow/canary. 현재 source inventory 기능만 구현한 것을 전체 리팩터링 완료나 검색 개선으로 보고하지 않는다.

## 최종 검증

- 최종 전체 회귀 명령: `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_knowledge_test_20261016 .venv/bin/python -m pytest -q`.
- 출력: `969 passed, 2 xfailed, 6 subtests passed in 187.38s`. 전체 재실행에서 초기 두 환경 실패를 포함해 정상 통과.
- unit25 및 Ruff clean. 실제 export CLI는 bounded timeout으로 unknown/incomplete/exit2를 반환; raw 전수 집계 성공으로 보고하지 않는다.
- WBS 순서를 KD→K0→K1→K2→K3→K4→K5→K6→K7→K8로 정렬. K1은 5.4.6.3 doing, 설계 KD는 5.4.6.1 done. 전체 리팩터링 완료는 아니다.
- 검증된 첫 tranche를 커밋·main push하고 standing policy의 request_backend_deploy로 예약한다. 예약 ID·최종 활성화 상태는 별도 앱 기록/사용자 보고로 남기며 예약 수락을 배포 성공으로 기록하지 않는다.
