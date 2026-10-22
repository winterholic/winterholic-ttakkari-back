# KB 설계 v2 수정 반영 독립 확인

- 검토일: 2026-10-21
- 검토 대상 수정 SHA: `460237722654d6cf1d540f321eae504e63028175`
- upstream: `origin/docs/kbv2-review-amendments` (새 clone에서 fetch 후 고정 SHA를 detached checkout)
- 독립 clone: `/tmp/kbv2-amend-review`
- 원 검토 기준: `8e3dc2c11f25098bc2fd1fee3886b6c235b5651f`; 원 검토 `approve-with-changes`
- 공유 체크아웃과 git 설정은 건드리지 않았고, 소스·테스트·migration·하네스 구현, commit, push, 배포는 하지 않았다. 이전 outbox 경로에 접근하거나 우회하지 않았다.

## 판정

**조건부 승인 — KB-E0 검색 품질 평가 하네스의 설계·구현 착수까지 가능.** 원 지적 P1–13의 설계 해법은 모두 대응하는 반례와 책임 단위를 갖췄고, F1–24/F9a도 핵심 의미를 축소하지 않고 수용하거나 명시된 대안을 택했다. 수정은 설계의 구체성을 높였지만 실제 DB·검색·모델·운영의 검증을 통과한 뜻은 아니다.

KB-E0에서는 평가 실행기·고정 정책/설정 manifest·정답/출처 anchor 표현·지연/자원 계측 인터페이스·실패 분류·통계 판정 로직을 구축할 수 있다. 공개 세트 adapter와 기준선도 권리 manifest 확인을 전제로 독립된 회귀 감시로 구성할 수 있다. 공개 세트만의 통과를 일반화, 사용자 승인 또는 최종 통과로 부르면 안 된다. 사람 검토 없는 최종 통과는 허용되지 않는다.

현재 설계는 사람 답변을 기다리지 않고 평가 하네스 자체를 만들 수 있게 분리했다. 사람 확인 holdout은 E1b의 최종 품질 gate에 남아 있고, 설계·하네스·공개 기준선 개발을 막지 않는다. 따라서 사용자에게 선행 답변을 요청할 이유는 없다.

**승인하지 않는 범위:** E0 완료 판정, 인덱스/모델 선택(E0.5), KB3 이후의 검색·수집·원장 단위, 운영 배포, 전체 도서관의 최종 품질 승인. 이들은 아래 계측 및 실 DB 검증 게이트를 통과해야 한다.

## P1–13 개별 확인

| 항목 | 설계 반영 및 반례 대조 | 판정 / 남은 차단 |
|---|---|---|
| P1 통계·holdout | 질의 독립 bootstrap을 폐기하고 문서/중복/번역 연결 group bootstrap, 사전 고정 Δ=.05, Holm, power 미달 `inconclusive`, one-use holdout을 명시. 112문항 충분성 주장을 폐기하고 abstain 최소 40+40도 충분성 보증이 아니라고 제한. | **설계 수용.** 실제 상관·불균등 cluster·power/CI 구현과 사람 확인 holdout은 E0/E1b 차단. 공개 세트로 대체 불가. |
| P2 gold·anchor | passage ID/offset을 정답 정체성으로 쓰지 않고 doc key+원문 인용/hash+출처 revision·시점으로 remap. 출처 변경 시 자동 유효화 금지, ambiguous/invalid를 분모에서 숨기지 않음. chunk 이동·NFKC 충돌·반복 인용 반례가 구체적. | **설계 수용.** KB5 정규화/분할 뒤 실제 anchor remap 및 E1b 사람 확인 전 자체 holdout gate 차단. |
| P3 지연 | 부하 초과를 면제하지 않고 AB/BA 기준선, queue/service/stage 분리, 상대 p95 ratio CI와 absolute SLO를 별도 판정. 공통으로 느린 시스템·비교불가 profile도 통과로 둔갑하지 않음. | **설계 수용.** E0 계측·pilot에서 profile 및 ratio CI를 실제 검증하기 전 성능 주장은 미확인. |
| P4 재현성 | cache replay는 정확 일치, 실추론은 사전 고정 epsilon/near-tie 기준으로 구분하고 HNSW rebuild 품질을 별도 취급. cache-key 변조, 모델/N 변경을 반례로 둠. | **설계 수용.** 구현된 cache와 실추론 분포 검증 전 재현성 통과 아님. |
| P5 관측 ledger | source hash/revision과 observation 사건을 분리하고 event time과 receipt time을 구분. A→B→A, A→A, 늦은 과거 snapshot, 동률·시계 이상·중복 전달의 기대 current를 명시. | **설계 수용.** 실제 DB 동시성/시계/멱등 검증 및 collector stable observation identity 계약은 KB4/KB5 차단. |
| P6 NFC 계약 | 삭제·공백 정리 뒤 최종 NFC→hash 순서와 normalizer version을 producer 계약으로 고정. 기존 producer는 격리·재발행, 소비자 재해시 금지. 4개 결합 문자 반례와 CRLF/공백/NUL fixture 포함. | **설계 수용, producer 선행조건.** collector 소유 코드/계약 변경 및 fixture가 확인되기 전 KB5 strict intake 활성화 차단. E0 하네스는 가능. |
| P7 pin·삭제·복구 | blob pin 소유자를 collector로 유지해 단방향 의존을 보존. 권리 철회 우선, 제한 deletion role/function, 전체 파생물/구세대/백업/export 복구 fence, crash 재개 및 물리 제거와 접근 차단 상태 구분. GUC 위조·권한 우회 반례 포함. | **설계 수용.** API·DB 권한·삭제/복구 end-to-end 실증 전 수집 영구 보존과 삭제 완결 보증 불가. KB4/5/6 차단. |
| P8 검색 선택 | Tantivy 채택을 철회하고 PG 앱-BM25/Tantivy/가능시 pg_search를 동일 query policy·실문서 100만·동일 gold로 bake-off. 19.71ms/.878 대 8.27ms/.851은 정책이 달라 우열 증명이 아니라고 유지. | **설계 수용, 엔진 선택 보류.** E0.5 실측 전 엔진·제품 의존성 확정 금지. BQ도 full HNSW와 비교 전 미확정. |
| P9 commit-order outbox | 모든 index-affecting writer가 공통 transaction lock을 seq 전·membership 변경 전부터 commit까지 취득한다는 조건을 명시하고, 멱등 delete/add replay·durable sidecar/checkpoint 순서와 crash 구간을 정의. lock 누락/CACHE32/seq 선할당을 negative control로 지정. | **설계 대안 수용.** 잠금/권한 경로 강제, 두 connection 역전, 모든 crash 경계·재생·digest를 실제 PostgreSQL/Tantivy에서 통과하기 전 외부 투영 활성화 차단. 추상 toy model은 DB 증거가 아님. |
| P10 모델·메모리·장애 | 모델 해시당 host 단일 서비스 소유, 세대 자식 금지, lease/drain 및 구·신 모델 공존 예산, 완성 입력 token window와 항목별 오류 격리, CPU fallback opt-in/미검증 거부, swap-in·GPU 측정 제한을 명시. | **설계 수용.** 메모리/GPU/CPU/장애 profile은 미측정. KB6 실제 서비스/세대·swap 관측 전 용량·처리량 주장 금지. |
| P11 no-answer·언어별 coverage | query/source 언어쌍별 dev 보정, near-miss, false strong/false abstain 분리, 후보 수 의존·표본 부족 unknown을 명시. GNI/GNP·단위/부정 등 구체 반례. | **설계 수용.** no-answer 전체 corpus 부재를 기계가 증명했다고 하지 않으며 후보 pool 및 corpus 범위를 기록. 사람 검토 holdout/언어쌍별 power 전 최종 none 품질 판정 차단. |
| P12 필터·세대 | auth/scope/current/domain을 후보 생성 전 적용, history 별도 모드, filtered exact 기준, ANN iterative scan 한계·1% visibility와 구 revision 반례, tuple별 build/reader lease·GC를 명시. | **설계 수용.** filtered exact 비교, 1%/90% stale/두 세대 동시 analyzer 실 DB 통과 전 ANN/세대 호환 판정 차단. |
| P13 명제·재개·충돌 | proposition key UNIQUE를 유지하되 닫힘과 동일 claim의 reopen event를 구분. 인용 재탐색 후에도 못 찾으면 `evidence_lost` review queue, 자동 철회 금지. conflicts는 추출 snapshot 범위·미추출량을 표시하고 `kb_get` 추출은 예산 있는 명시 쓰기 경로. | **설계 대안 수용.** 실제 DB/API 전이·동시 version·부분 추출의 `conflicts=[]` 반례 통과 전 KB10 차단. |

## F1–24 및 F9a 개별 대응 확인

| 지적 | 확인한 설계 대안 / 결론 | 남은 착수·통과 차단 |
|---|---|---|
| F1 | P5 observation ledger. 동일 내용 A→A도 검증 시각을 남기고 A→B→A 복귀를 재현. **수용** | KB4 실제 DB |
| F2 | P9 공통 lock-before-seq와 멱등 pid replay, durable checkpoint. xid 단독보다 보수적 대안. **대안 수용** | KB6 lock/kill/replay |
| F3 | P8 bake-off로 Tantivy 확정 철회. 실자료 수치의 query policy 차이·positive enrichment·avgdl 근사를 공개. **수용/선택 보류** | E0.5 실측 전 선택 불가 |
| F4 | P1 power/cluster bootstrap/Holm/inconclusive, 작은 셀 veto 제거. **수용** | E0 실제 통계 구현 |
| F5 | P2 stable quote anchor, revision 출처 근거 유지, chunker 이후 자체 holdout. **수용** | KB5 후 E1b |
| F6 | P3 고부하 판정 유지, 절대+상대 지연 분리. **수용** | E0 계측·실측 |
| F7 | P10 단일 host 모델 소유, Kiwi 세대 수·cache·swap-in 예산. **수용** | KB6 실 메모리/CPU/GPU |
| F8 | P13 같은 key reopen event, 증거 유실 자동 철회 금지. **대안 수용** | KB10 실제 전이 |
| F9 | `conflict_check`에 complete/partial/not_run, 추출·후보 수·scope/build를 노출; 추가 추출은 예산 예약 쓰기 호출. **수용** | KB10 API/부분 추출 검증 |
| F9a | P11 language-pair calibration, 근접 오답, 두 오류의 분모 분리. 기존 단일 τ와 18/20을 운영 주장에 쓰지 않음. **수용** | 사람 holdout 및 충분한 표본 |
| F10 | P6 producer final NFC/version/reissue를 명시. collector 구현은 별도 소유 변경 요청으로 분리. **수용** | collector 계약/코드 전 KB5 활성화 불가 |
| F11 | P7 pin 방향·권리우선 tombstone·전체 파생물/복구 fence. **강화 대안 수용** | owner API/권한/삭제 실증 |
| F12 | benchmark-family 학습 노출·split/checkpoint/XOR 중복 미확인을 유지. code/data/qrels/corpus 권리를 분리하고 공개 점수는 회귀 감시로 제한. **수용, 권리·노출 보류 유지** | E1a 권리 manifest; 공개 성능만의 최종 통과 금지 |
| F13 | cache replay와 live inference 허용오차를 분리하고 tolerance는 미실증 정책으로 표시. **대안 수용** | E0 pilot |
| F14 | tokenize된 완전 입력 기준 window/token limit, parent/span과 항목 오류 격리. **수용** | KB6 실제 token boundary/배치 시험 |
| F15 | scope/current 사전 필터, filtered exact recall 및 iterative scan·fallback 한계 명시. **수용** | KB6/7 DB 성능 |
| F16 | C3 확장 후보·가중치 상한, 모호 별칭은 도메인 조건, OOV·수치/단위/부정 평가. **수용** | KB9 골든 평가 |
| F17 | analyzer/model/query policy별 build tuple과 reader lease, 구세대 GC 방지. **수용** | KB6 세대 교차 시험 |
| F18 | .989가 1,815 문서간 overlap임을 고정하고 실벡터 100만 전 미확정. **수용** | E0.5 |
| F19 | 자체 산문 throughput 기준, ≥80% 상대 기준. 2.39시간과 4 worker는 계산/이상적 외삽으로 표시. **수용** | KB3 고정 corpus 측정 |
| F20 | Tantivy wheel·Python 3.14 불확인과 Kiwi LICENSE/NOTICE를 구별. 모델/data 권리 별도. **수용** | E0.5 이후 설치 matrix·권리 확인 |
| F21 | 12 질문유형+추가 반례 태그, 각 split 8개는 수집 목표일 뿐 power 보증 아님. **수용** | E1b 표본/사람 확인 |
| F22 | 분석 토큰 오프셋을 저장본문 offset으로 사용 금지, NFKC 길이 변화 골든. **수용** | KB3/5 실제 테스트 |
| F23 | E0.5·도구 선택 30문항·모바일·독립 반례를 순서에 반영. E1b 지연이 E0/공개 기준선을 막지 않음. **수용** | E0.5/E1b 자체 gate |
| F24 | 공통 원전/전재·near-duplicate를 independence basis로 추적하고, 조건별 provenance 상속, 인용 byte 검증과 의미 지지 분리, trace 최소화. **수용, 실제 정책 실행은 미검증** | KB5/7/10 독립·권리·개인정보 검증 |

## 중요한 주장별 재검토

- **PG 대 Tantivy:** 원 검토 raw의 213질의, 실텍스트 100만 결과가 보존됐다. PG filtered BM25 `p95=19.71ms, Recall@100=.878`; Tantivy `8.27ms, .851`; PG `ts_rank_cd` `3501.07ms, .391`. PG와 Tantivy query policy가 다르고 PG의 positive enrichment·평균 길이 근사·부하/작은 질의 집합이 남아 있으므로 승자는 없다. 같은 정책/같은 gold bake-off 요구는 원 P8을 충족한다.
- **BQ 및 교차 언어:** BQ .989는 1,815개 문서간 top-10 overlap이지 검색 정확도가 아니다. 10만 질문→passage C200 .997도 방향 근거다. 18/20 교차 언어 결과는 77후보 소표본 스모크다. 둘 다 실벡터 100만 및 대표 질의 gold 결과처럼 표현하지 않는다.
- **원본 보존·collector:** kb는 blob 자체를 소유하는 대신 collector의 pin API를 부르고 수용 receipt와 결속한다. rights 삭제가 pin보다 우선하고 파생물/복제본/backup/복원까지 대상이다. collector 코드 수정은 KB 소유인 것처럼 꾸미지 않고 별도 변경 요청. 다만 계약과 실제 권한·복구 경로가 만들어지기 전에는 source retention을 구현 완료로 셀 수 없다.
- **정규화·좌표:** producer가 마지막 NFC를 하고 hash를 내며 kb는 재작성하지 않는다. 구 producer와 신규 strict contract를 분리. NFKC 분석 좌표를 원문 좌표로 재활용하지 않고 quote를 저장 revision에서 재탐색한다. 두 경계는 별도 producer·KB5 fixture로 검증하도록 되어 있다.
- **no-answer·대표성·최종 평가:** 답 없음/있음 분모, false strong/false abstain, near-miss, language pair, 표본/검정력 부족 `inconclusive`가 구분된다. answer absence를 자동 증명하지 않으며 사람 확인 자체 holdout을 유지한다. 도메인·질문 유형 표본 목표와 group 분리는 있지만 대표성은 표본 설계 및 사람 판단이 실제로 닫아야 한다. 사용자가 허용하지 않은 공개 세트 잠정통과를 최종 gate로 승격할 수 없다.
- **라이선스·오염:** BGE-M3 학습 계열·checkpoint/split overlap과 XOR/Mr.TyDi 동일성은 미확인으로 남겼다. 코드, 논문, dataset, qrels, corpus 권리를 분리한다. 이 미확인이 KB-E0 harness 설계를 막지는 않지만 해당 데이터 사용 전에 manifest의 권리 확인을 막을 수 없다.
- **리소스·복구:** 모델 소유·세대 lease·token window·GPU/RSS 구분·swap-in·CPU profile·degraded/재시작을 실행 단위별 검증으로 남겼다. GPU/CPU/메모리와 실제 DB/kill/replay는 이번에 실행되지 않았다.

## 착수 게이트

1. **지금 가능 — KB-E0 평가 하네스만:** 평가 스키마/runner, replay/cache 정책, cluster 통계 판정, 단계별 계측과 실패 분류, 권리 메타데이터가 포함되는 데이터 loader를 만들고 독립 검증할 수 있다. 아직 제품 검색·수집 구현으로 확대하지 않는다.
2. **E1a 공개 기준선:** 각 corpus/qrels/license 원문 URL·버전·권리 확인을 기록한 후, 회귀 감시 목적으로만 실행 가능. 공개 점수만으로 독립 일반화나 최종 pass를 내지 않는다.
3. **KB-E0.5 검색 선택:** 하네스와 고정 gold/policy가 준비된 뒤, 동등한 PG/Tantivy/(가능한 pg_search) 질의 정책·실자료 100만 비교를 실행. 선택은 결과가 통과한 뒤만 가능. PG BM25와 Tantivy의 원래 불공정 비교를 재사용해 선택하지 않는다.
4. **KB5 intake strict 활성화:** collector의 마지막 NFC, normalizer 버전, observation identity/event-time 및 pin 연동 계약과 실제 intake fixture/DB 검증 필요. collector 소유 변경을 사용자 의도 없이 kb에서 구현했다고 간주하지 않는다.
5. **KB4/6/7/10 및 최종 quality gate:** ledger A→B→A·동시성, commit 순서 outbox/crash/replay, 권리 tombstone 복구, 모델·GPU·메모리·세대/필터, no-answer threshold 및 partial conflict, claim reopen·independence proof를 실제 DB/API/서비스에서 독립 검증해야 한다. 마지막 평가 pass는 사람 확인 자체 holdout과 사전 등록된 기준을 요구한다. 사람 답변은 이 하네스 구축의 선행조건이 아니다.

## 재현 실행 기록

### 현재 Run에서 직접 실행

별도 clone에서 아래 명령을 실행했다. 원 evidence clone은 `/tmp/kbv2-review`이며 원 outbox에는 접근하지 않았다.

```text
$ git clone --no-checkout https://github.com/winterholic/winterholic-ttakkari-back /tmp/kbv2-amend-review
Cloning into '/tmp/kbv2-amend-review'...

$ git -C /tmp/kbv2-amend-review fetch origin refs/heads/docs/kbv2-review-amendments
From https://github.com/winterholic/winterholic-ttakkari-back
 * branch            docs/kbv2-review-amendments -> FETCH_HEAD

$ git -C /tmp/kbv2-amend-review checkout --detach 460237722654d6cf1d540f321eae504e63028175
HEAD is now at 4602377 docs(kb): v2 독립 검토 P1–13 설계 보완과 원본 증거 보존

$ git -C /tmp/kbv2-amend-review rev-parse HEAD
460237722654d6cf1d540f321eae504e63028175

$ python3 docs/design/kb-v2/reviews/check_design_delivery.py /tmp/kbv2-review/docs/design/kb-v2/reviews
 evidence: 18/18 exact sha256+size; source byte comparison: True
 coverage: P1-13=13/13, F1-24=24/24 plus F9a; negative controls rejected
 links: 27 local file targets exist (historical original review excluded)
 historical raw: 213 queries; PG BM25 19.71/.878, Tantivy 8.27/.851, ts_rank_cd 3501.07/.391
 planning only: n_eff=353; 0/40 one-sided upper=0.072158; Kiwi=2.3889h; CPU=45.5373h
 Unicode model: 4/4 early-NFC counterexamples; final NFC fixed points; NFKC: {'㈜': '(주)', '…': '...', 'ﬁ': 'fi', 'x²': 'x2', 'µ': 'μ', '℃': '°C'}
 toy seq model: unguarded loss 2/30, lock-before-allocation serialized loss 0/10
 toy observations: A->B->A current A; late event0 unchanged; A->A freshness advances
 UNEXECUTED: actual PostgreSQL/kill/replay, inference/GPU/CPU, million benchmark, product harness, deployment
 exit=0
```

18개 원본 evidence 별로 source byte, manifest byte count, SHA-256을 재비교했다. 이 명령의 출력 결과는 18행 모두 `MATCH`; 이름·size·SHA는 다음 표에 그대로 기록한다. “해시 일치”는 보존 무결성만 증명하고 그 원 측정의 방법론/대표성을 새로 검증하지 않는다.

| 원본 경로 | bytes | SHA-256 | 비교 |
|---|---:|---|---|
| evidence/dense_eval.py | 6012 | 383655bf83f9f25dfbf8dd21a0e004ad0d4015c330153d2fd4e3b973d358a79b | MATCH |
| evidence/dense_eval_100k.out.txt | 1374 | bdf681fcd293534987d2af7f0ef7e11dadd14f030b4af5c5d5800e59c830f9b0 | MATCH |
| evidence/det.py | 1324 | 1c6467cce6168227881776e93512eecfc20241035a03ccc21131946e42eaf276 | MATCH |
| evidence/embed.py | 1402 | d05edbd5d75f213472bb473d4be81dccfbade356d1fa791e25f312f3973ffb22 | MATCH |
| evidence/gatepower.py | 1662 | 50742500b1e961ee14042c9c410e323a33560815e8e875631992ab840119b948 | MATCH |
| evidence/items.py | 7437 | 79950822fd6bb971d32ac2d94183a089b0db8f4fce00dee0f371b15c0242ae90 | MATCH |
| evidence/lexcmp.py | 3940 | c73f29430b9835bdd34f83ef48a3800137eba62681ff98d90336414b144e1e1b | MATCH |
| evidence/lexcmp1m.py | 3944 | 89f7a9e75a4002a69cbbbc49ca353d468bd223a921d71fa2ca1f0708b35fc4a6 | MATCH |
| evidence/lexcmp_100k.out.txt | 511 | 620dd6a43e1553ea60225ff8706ea8e9df2a2d5477a3f987c5fc197a640e960d | MATCH |
| evidence/lexcmp_1m.out.txt | 688 | 8f1fcab4e52fe51240020cec42d1efead2be77b217f238a486590e7811eed885 | MATCH |
| evidence/load_pg.sql | 651 | 4384705005434c554095c58f37a297b4aa3388f739b42f50538572aa957cdb55 | MATCH |
| evidence/prep.py | 2190 | 0c8f9aab58ba39006827c63c0a332776c46a2916e8f7c5f11aa083ac1332ee57 | MATCH |
| evidence/repro_misc.out.txt | 989 | d50c13b703dbeeff01923e7ee233e2736ab1b6a8c7159c9294393bb66fc04c23 | MATCH |
| evidence/scores.json | 1212 | 6c0922d1022587902e6bc592c921dc6d6ff90de5c2b56ad32d442d9db767e723 | MATCH |
| evidence/tv_test.py | 1173 | 1e787a9c5a2501ef63961f26cb206714983c644b7bddb9c45cf9f7fa92a9623e | MATCH |
| evidence/xling.py | 4521 | aea6bf765229df2fb70203dd63a2cf37d1db635873dd276d6f8bfc323a2cf7e9 | MATCH |
| evidence/xling_results.out.txt | 943 | 4065cccc62048136505075c4bde42d996f40d15a9aba1d146540bb1a01528eef | MATCH |
| v2-design-review.md | 51084 | 6c0b45c696a21d1a2d57142b116c621a23e5d6bdca3ef60450d082dd7371bbe1 | MATCH |

```text
TOTAL 18/18 byte-identical source, size, sha256
```

### 미실행 및 범위

- 현재 Run에서는 `pytest`, PostgreSQL `initdb`, benchmark, model/GPU/CPU, kill/replay, production harness, deployment, push를 실행하지 않았다.
- 수정 자료의 기존 pre-push 기록은 문서 브랜치 게시를 위한 별도 codex command sandbox 실행으로, 첫 시도는 `uv sync` cache 경로에서 `Operation not permitted`; 임시 cache로 재실행한 main contract gate는 `initdb` 시작 단계에서 실패하고 내부 stderr가 보존되지 않았다. 문서상 알려진 `shmget Operation not permitted`와 부합하는 환경 범위의 문제이나 이번에 재실행하지 않았다. 이는 **KB 제품 회귀도, 배포 backend의 실패 증거도 아니다**. 계약 pytest는 시작되지 않아 통과 여부 미확인이다.
- `amendment-validation.txt`에는 링크 수 25로 기록돼 있으나 이번 고정 SHA에서 같은 checker를 실행한 출력은 27이고 모두 존재 확인을 통과했다. 숫자 기록이 stale한 것으로 보이며 링크 무결성 오류는 관측되지 않았다.
- 작은 sequence/unicode 모델은 설계 전제 반례를 확인한 추상 계산일 뿐 실제 PostgreSQL, 파일시스템 내구성, 세대 drain 동작을 증명하지 않는다.

## 최종 결론

P1–13 및 F1–24/F9a의 **설계 반영 확인은 통과**한다. 승인 범위는 평가 하네스 우선의 KB-E0 착수까지다. 엔진 선택·데이터 수용·검색/원장 제품 구현과 최종 배포 품질은 승인하지 않았고, 각 실제 검증·권리 확인·사람 holdout 게이트가 남아 있다. 사람의 답변은 E0 하네스 구축을 막지 않으며 공개 세트만으로 최종 품질 pass를 만들 수도 없다.
