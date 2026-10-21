# v2 독립 검토 대응표 — KB-E0 로직 완료 승인, 제품검색 미승인

현재: KB-E0 로직이 [독립 확인 4회차](kb-e0-independent-review-v4.md)에서 완료 승인됐다(DB·UI·추론 제외, [E0 완료 기록](kb-e0-completion.md)). 그 전에는 [수정 반영 독립 확인](amendment-independent-review.md)에서 KB-E0 착수만 조건부 승인했다(KB-E0 착수 조건부 승인, 제품검색 미승인). 하네스 구현·검증 진행은 [E0 기록](../../../work-history/2026-10-22-kb-e0-harness.md)을 따른다. 아래는 수정안 작성 당시의 대응·미실행 기록이며 과거 판정을 소급 변경하지 않는다.

## 수정안 작성 당시 기록

2026-10-21. 검토 Run `78470bf8-41e6-4721-aa1c-20a853841cb3`, 대상 SHA `8e3dc2c11f25098bc2fd1fee3886b6c235b5651f`. 원 판정 **approve-with-changes**를 그대로 보존한다. 검토자는 P1–4 후 E0 시작, 나머지는 해당 단위 전 해소, 전체 재설계 재검토 불필요를 제안했다. 사용자는 이번에 P1–13 모두 설계 해법을 명시하고 반영만 독립 확인한 뒤 부모가 E0를 시작하도록 지시했다. **이번에는 문서·근거만 변경하며 제품/하네스 구현 금지**다.

수집 플랫폼 분리 결정(2026-10-20, memory source session `cd62951a-bb9f-45f9-8c5b-4545863e704d`)과 품질 지시(2026-10-21, source session `3c35db30-998f-4c71-beb9-8149ca27683f`)를 recall로 확인했다. collector는 독립 범용 플랫폼, kb는 표준 계약 소비·저장/정본/관계/검색 소유다. 설계 확정/독립 검토→평가 하네스 선행→단위별 실제 DB 독립 검증을 유지하며 핵심 설계/원장/검색은 경량 모델에 넘기지 않는다.

대응 분류에서 **수용/대안**은 문서에 해법을 명시했다는 뜻이다. **보류**는 구현/측정/외부 계약 확인까지 해당 단위의 완료/활성화를 차단한다는 뜻이며 지적을 무시한다는 뜻이 아니다. 아래 모든 실행 완료는 미실행이다. 이번 문서의 논리 검토를 독립 검증 통과로 표시하지 않는다.

## P1–13 설계 해법·반례·완료 책임

| 항목 | 대응·근거 | 활성 설계 위치 | 반례·완료 단위/책임 |
|---|---|---|---|
| P1 | 수용. 질의 독립 CI·작은 셀 하락금지 폐기. Δ=.05 정책+cluster power·Holm·one-use holdout·inconclusive | [§12 P1](../knowledge-library-design-v2.md#p1--f4-통계-판정과-holdout-사용), §4.3–4.7 | 상관/불균등 group·중립 power·40미만 abstain. E0 평가 담당, E1b 표본/확인 |
| P2 | 수용. doc_key/quote anchor+출처 revision/hash/time 보존. 원문 변경 invalid, chunker 변경 remap | §12 P2, §4.4 | 재분할·반복인용·문서정정·NFKC 좌표. KB5/E1b 평가 담당 |
| P3 | 수용. 부하 제외 폐기, 동시 control AB/BA·queue/service·절대/상대 분리 | §12 P3, §4.6 | control도 느림·후보만30% 증가. E0 타이머, KB6/7 성능 담당 |
| P4 | 대안 구체화. 캐시 고정 replay 정확일치·실추론 ε/near-tie·HNSW rebuild 품질 분리 | §12 P4 | cache key 변조·tie 경계·모델/N 변경. E0/KB6 평가 담당 |
| P5 | 수용. 내용 revision과 모든 관측 ledger 분리·event/receipt time | §12 P5, §7.2–7.4 | A→B→A/A→A/늦은 snapshot/동률. KB4 원장 담당 |
| P6 | 수용. producer 마지막 NFC+버전/해시/재발행 계약. consumer 재작성 금지 | §12 P6, §7.3 | e+soft hyphen+accent 등4개. KB5 어댑터; collector 코드 변경 별도 소유자 |
| P7 | 대안 강화. collector 소유 pin API·rights 우선·승인 deletion role/함수·전파/복원 fence | §12 P7, §6.8 | GUC 위조 거부·삭제 중 crash·old build/backup 부활0. KB4/5/6 원장·운영 담당 |
| P8 | 수용. Tantivy 확정 철회, 동일 정책 실자료100만 PG BM25/Tantivy/가능시 pg_search bake-off | §12 P8, V3/§5.3/§9 | positive enrichment·avgdl·OR폭·정책 동등성. E0.5 검색/평가 담당; 선택은 보류 |
| P9 | 대안 선택. xid 미채택; 모든 writer 동일 xact lock·seq CACHE1/선할당 금지·멱등 pid replay·durable cp | §12 P9, §3.9 | 실제 commit 역전·CACHE32 음성·commit 중 kill·중복 replay. KB6 DB/검색 담당 |
| P10 | 수용/구체화. 외부 host 단일 소유·세대 child 금지·장애/CPU 검증 profile·token window·배치 격리·3세대/swap-in 예산 | §12 P10, §3.8–3.9 | 서버kill·2048 경계·31+1 배치·구/신모델·3세대. KB6 운영/검색 담당 |
| P11 | 수용. 언어쌍/N별 dev calibration·near-miss·false strong/false abstain 분리 | §12 P11, §3.6 | ko/en score 이동·동일주제 오답·표본 부족unknown. KB7 검색/평가 담당 |
| P12 | 수용. 각 경로 auth/current 사전조건·history 분리·filtered exact recall·tuple build/reader lease | §12 P12, §3.3/§3.9/§5.5 | scope10/1%·과거90%·다른 analyzer 두세대·GC. KB6/7 검색/세대 담당 |
| P13 | 대안 선택. key UNIQUE 유지+같은 행 reopen event. evidence_lost 검토·재탐색, partial conflict_check | §12 P13, §6.6/§3.7 | 종료후 같은 key·404·문단이동·미추출 반대문장. KB10 원장 담당 |

## F1–24 전체 대응

| 지적 | 분류 | 근거·변경 및 잔여 차단 |
|---|---|---|
| F1 | 수용 | P5 ledger로 원본 중복과 수집 사건을 분리. KB4 실제 DB 현재성 시험 전 미완 |
| F2 | 대안 | P9 전역 직렬화+캐시1 sequence·writer 권한·멱등 replay. xid 단독은 안전 조건 부족으로 미채택. KB6 kill/역전 검증 전 차단 |
| F3 | 수용+선택 보류 | P8. PG 전체 불가/Tantivy 확정 철회. E0.5 동일 정책·실텍스트100만·gold 뒤 엔진 추가 확정 |
| F4 | 수용 | P1. 초기112개는 power 보증 아님. 유형 셀은 보고, abstain 최소수·power 미달 inconclusive |
| F5 | 수용 | P2. anchor가 passage를 재매핑, 출처 revision 근거 유지·원문 변경 invalid. E1b 최종 확인을 KB5/6 뒤로 |
| F6 | 수용 | P3. 공유 부하가 높아도 absolute_fail/relative 판정. 관측 실패만 unverified |
| F7 | 수용 | P10. host 단일 외부 서비스·Kiwi3세대·cache·Metal/OS working set·swap-in 예산. 실제 메모리/CPU는 여전히 미실행 |
| F8 | 대안 | P13. proposition_key UNIQUE 유지·종결 episode를 reopen event로 재개. evidence_lost 자동 철회 금지 |
| F9 | 수용 | P13. conflict_check snapshot 범위/partial·미추출수, kb_get 명시 추출은 예산 쓰기 경로 |
| F9a | 수용 | P11. 언어쌍별 보정·near-miss·두 오류 분리, 검토20문항의 최적 τ 재사용 금지 |
| F10 | 수용 | P6. 최종 NFC 생산자 계약 선택, 버전/재발행/격리. collector 소유 코드 변경은 [별도 요청](collector-contract-followups.md) |
| F11 | 대안 강화 | P7. session variable 우회안을 쓰지 않고 role/approved request/tx-bound 함수 경로. pin 소유·권리삭제·파생물/replica/backup 복구까지 명시 |
| F12 | 수용+확인 보류 | §12 추가 계약. MIRACL/Mr.TyDi 계열 학습 노출, split/checkpoint/XOR 겹침 미확인 유지. code/data/qrels/corpus 권리 분리. 공개 통과는 일반화/사용자 승인 아님. E1a 권리·E1b 사람 확인 차단 유지 |
| F13 | 대안 | P4. cached replay만 정확일치, live inference ε·near-tie. 수치 tolerance는 초기 정책이고 pilot 미실행 |
| F14 | 수용 | P10. 완성 input tokenize·window parent/span·slot/b/ub·항목 실패 격리. 긴 passage 자르기로 숨기지 않음 |
| F15 | 수용 | P12. scope/current 적격 membership·PG 최종권한 확인·dense iterative scan/filtered exact 대조. 단순 사후200개 필터 폐기 |
| F16 | 수용 | §3.3/§12 추가 계약. C3 weight≤.5·단독≤25%, 모호별칭 도메인 필수, OOV100·수치/단위·부정·질의별 순위 손익. KB9 |
| F17 | 수용 | P12. analyzer/model/query policy별 build·세대 supported tuple·reader lease, 구모델 상주 예산/직렬화. KB6 |
| F18 | 수용+선택 보류 | P8. .989는1815개 문서간 overlap. full/BQ 실벡터100만 선택 시험 전 미확정 |
| F19 | 수용 | §5.4/§12 추가 계약. Kiwi30만자 gate 폐기, 자체 산문 baseline80%. 2.39시간 단일은 외삽, 프로세스 RSS 포함. KB3 |
| F20 | 수용+확인 보류 | §12 추가 계약. Tantivy cp313/비abi3, 3.14 휠 미확인; Kiwi LICENSE/NOTICE 유지. 선택후 KB6 설치 matrix |
| F21 | 수용 | §4.3 확장태그·각 split 최소8 수집 목표·report 중심. P1/P11 abstain의 별도 최소/검정력. E1b |
| F22 | 수용 | §12 추가 계약. 저장 본문 좌표 재탐색; NFKC길이/과학 표기 충돌 골든. KB3/5 |
| F23 | 수용 | §9. E1a 공개 기준선→KB5 정규화/분할→KB6 pool→E1b 최종 사람확인. E0.5 선행·모바일·도구30문항·독자반례 먼저. 112개/2시간 미확인 추정 |
| F24 | 수용 | §6/§12 추가 계약. independence_basis·공통원전/전재, applies 조건상속·출처별 provenance, quote_hash_verified·문장선택/지지분리·personal trace 최소보존. KB5/7/10 |

## 근거 보존과 현재 실행 범위

- [원 보고서](v2-design-review.md)와 source-manifest의 **18개 원 파일**은 `/tmp/kbv2-review` 사본 바이트를 그대로 보존한다. `.py` 원 스크립트는 `.py.txt`로 저장하여 과거 측정 프로그램이 프로젝트 실행 코드/자동 포맷 대상이 되지 않게 했다. 이름 대응·sha256·size·원 사본 mtime·검토일/대상 SHA를 [manifest](source-manifest.json)에 남겼다. 원본의 정확 측정 시각이 없는 곳은 새로 발명하지 않는다. 모델·DB·corpus/vector binary는 포함하지 않는다.
- `lexcmp_1m.out.txt`: 213질의/실텍스트100만의 PG filtered BM2519.71ms/.878 vs Tantivy8.27ms/.851, PG ts_rank_cd3501.07ms/.391. query policy가 다름·positive enrichment·avgdl40근사·부하·소표본 조건을 유지한다. 원 수치를 수정하거나 이번 실측으로 재명명하지 않는다.
- 검토 원문 §6 미확인·미실행은 §12.1에 보존한다. 이번 새 실행은 checksum·문서 link/범위·산식/Unicode·작은 **추상 모델** 확인뿐이며 실제 PG·GPU·모델·kill 시험으로 해석하지 않는다. [검증 명령/출력](amendment-validation.txt)에 범위를 적는다.
- v1 두 사본과 ADR0063은 상태 배너로 역사 기록/현행 구현 사실/활성 v2의 대체관계를 분리했다. collector 경계 참조 중 기존 NFKC 동일 요구만 문서에서 바로잡고 코드 변경은 별도 반환했다.

다음은 부모의 **반영 여부 독립 확인**이다. 이번 작성자가 스스로 독립 승인하거나 E0/제품 구현을 시작하지 않는다.
