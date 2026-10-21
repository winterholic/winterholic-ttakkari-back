# 수집 단계 handler 작업 노트 (CP-S2-03 / S2-04 / S2-06 구현분)

2026-10-21 · 구현: `src/ttakkari/collector/` · 증거: `reports/collector/s2-03`, `s2-04`, `s2-06`. 독립 검증 전이며 WBS는 review로만 올린다.

## 범위

| WBS | 구현한 것 | 일부러 남긴 것 |
|---|---|---|
| S2-03 출처 정책 | `policy.py` SourcePolicySpec(허용 host, 용도별 permission, 공식 정책 URL·확인 시각·license·근거, 속도·깊이·페이지 상한, 원문 보존 여부). 서버 등록·append-only 버전(`collector_source_policies`, Alembic 0063). 정책 없음/미확인 용도는 거부. 샘플 10건 inventory(5 도메인 x ko/en) | 정책 등록 HTTP API(S2-07 `/sources`). 정책 원문을 사람이 다시 읽는 확인 |
| S2-04 fetch·추출 | `netguard.py`(URL 정규화, 비공개 주소 거부), `fetcher.py`(검증 IP 고정 연결, redirect 매 hop 재검사, 상한 bytes/time, 압축 해제 상한), `robots.py`(RFC 9309), `polite.py`(정책→cooldown→robots→간격 조절), `parse.py`(trafilatura, feed/sitemap/링크 탐색) | 실제 웹 스모크, IPv6 실접속, 브라우저·PDF·YouTube(미지원 상태로 기록) |
| S2-06 정규화·중복 | `textproc.py`(NFC 정규화·변환 단계 기록, 언어 식별, 정확 SHA와 MinHash 근사 후보), `blobs.py`(content-addressed 원본), `pipeline.py`(단계 연결, 엔벨로프 생성·검증) | 낮은 점수 hold 표본의 stratified reservoir 감사(회수 절차), 재처리 job |
| L0/L1 | `quality.py` L0 규칙 신호(hold만 생성), L1 `QualityScorer` port·`L1Policy`(off/shadow/gate) | L1 모델 탑재(다음 작업). 모델이 없으면 모든 축은 `unmeasured` |
| 평가 정책 | `profiles.py` general / news(최신성) / tech_docs(버전) | 신규 profile은 클래스 하나와 `PROFILES` 등록만 필요 |

S2-05(전역 예산·정산·캐시)는 건드리지 않았다. 이 경로는 LLM을 호출하지 않으므로 예산 예약이 필요 없다. teacher/judge를 붙일 때 S2-05가 선행이다.

## 흐름

job의 envelope에 `extensions["collector:request"]`가 있다. 없으면 `source.url` 하나를 seed로 쓴다.

```json
{"seeds": ["https://..."], "profile": "news", "profile_params": {"max_age_days": 7},
 "max_pages": 20, "max_depth": 0, "l1": {"mode": "off"}}
```

요청은 정책을 좁힐 수만 있고 넓힐 수 없다. 초과하면 job은 `quarantined/invalid_request`로 끝난다. envelope의 `rights`는 인증 주체가 보증하지 않으므로 복사하지 않고, 산출 envelope의 rights는 등록된 SourcePolicy에서만 만든다.

1. 정책 조회. 없음 → `policy_blocked/source_policy_missing`. store 미허용 → `policy_blocked/store_not_permitted`.
2. frontier(cursor)를 PAGE_SIZE(5)씩 처리한다. 한 URL: 정책(host·scheme·port·path) → 내구 cooldown → robots(24h 캐시) → 간격 조절 → GET(ETag/Last-Modified 조건부).
3. 원문 bytes를 그대로 보존(`cblob:sha256:<hex>`, 같은 bytes는 한 번). `store_raw=false`면 해시만 남기고 `unpreserved:` handle과 `raw_preserved:false`를 기록한다.
4. 종류 판별(html/text/feed/sitemap/미지원). feed·sitemap·HTML 링크는 후보 URL을 frontier에 넣는다(정책 통과분만, 중복 제거, 상한). feed/sitemap은 depth를 쓰지 않고 HTML 링크만 depth+1.
5. 추출(trafilatura) → NFC 정규화(변환 단계 수 기록) → 언어 → L0 → profile → (선택) L1.
6. 한 page 안의 쓰기를 `JobContext.commit_work` 한 트랜잭션으로 커밋한다: item, dedupe band, fetch 캐시, outbox(`export`는 accept만), 다음 cursor. attempt token으로 fence하고, 같은 tenant/scope 안의 중복 판정은 advisory lock으로 직렬화한다.

## 판정 의미

- `accept`: 권리·robots 통과, L0·profile·L1 hold 사유 없음. `export` outbox 행 생성.
- `hold`: 품질·profile·정확중복·미지원 미디어. 원문과 envelope는 보존되고 삭제하지 않는다. hold는 권리 차단이 아니다.
- `error`: 추출 실패(빈 본문 등). errors 비어 있지 않음.
- `blocked`: 정책/robots/SSRF/401·403. 원문이 없어 envelope도 없다. item 행에 사유가 남는다.
- 정확중복: 다른 URL에서 같은 정규화 본문이면 occurrence는 별도 행으로 남기고 hold + `exact_of`. 같은 URL의 새 revision은 중복이 아니다. 직전 fetch와 바이트가 같으면 `not_modified`(ETag 또는 내용 해시), A→B→A는 새 revision이다.
- 근사중복: MinHash(128, 문자 5-gram, LSH 16x8) 추정 Jaccard ≥ 0.80이면 후보로 연결하고 판정은 막지 않는다. `cluster_id`는 첫 구성원 item id이며 첫 구성원 자신은 null이다.
- 회계: `discovered == accepted + held + blocked + failed + not_modified + discovery + skipped_limit + 남은 frontier`. 정책 밖 링크는 `skipped_policy`로 따로 센다. 테스트가 매번 검사한다.
- 재시도: timeout/429/5xx만. URL당 최대 3회, host cooldown은 DB(`collector_host_state`)에 저장하고 Retry-After를 하한으로 쓴다. job은 `retry_wait`로 쉬고 이미 커밋된 page는 다시 하지 않는다.

## S2-02 독립 검증 반례 반영

취소 플래그가 먼저 기록됐는데 handler가 마지막 page 뒤에 `succeeded`로 끝나면 job이 succeeded로 남던 경합을 고쳤다. `jobs.finish`는 취소 요청이 있으면 succeeded/partial/retry_wait/budget_wait를 `cancelled`로 바꾸고(`last_error.finished_as`에 원래 상태 기록), failed/policy_blocked/quarantined는 실제 결과이므로 유지한다. 테스트: `tests/test_collector_jobs.py::test_cancel_recorded_before_the_attempt_ends_wins_even_if_the_work_finished`, `test_consume_cancel_after_the_last_page_commit_still_ends_cancelled`(고치기 전 코드에서 3건 실패 확인).

## 의존성 추가 이유

- `trafilatura>=2.3,<3`: 본문 추출 후보로 설계(architecture 5)에서 지정. 다운로더는 쓰지 않고 이미 받은 bytes에만 적용한다. 부수 설치: lxml, courlan, htmldate, justext, dateparser 등. 수집 후보 문서 구조 변화(parser drift)는 빈 추출 → `error` 판정으로 드러난다.
- `py3langid>=0.4,<1`: 언어 식별. numpy만 필요하고 모델이 작으며(약 2MB) 결정적이다. 한국어·일본어는 문자 스크립트 규칙으로 먼저 정한다. lingua(모델 대용량)·langdetect(비결정적)는 선택하지 않았다.
- 넣지 않은 것: datasketch(MinHash는 numpy+blake2b로 자체 구현, 결정적), robots 파서 패키지(urllib.robotparser는 first-match·와일드카드 미지원이라 RFC 9309 자체 구현).

## 미검증·한계 (숨기지 않는다)

- 공용 인터넷 실접속 스모크 미실행. 모든 fetch 테스트는 로컬 fixture 서버와 로컬 self-signed 인증서다.
- sample inventory의 URL·license·robots 관찰은 WebFetch(요약 모델 경유) 결과다. 원문 전수 대조가 아니며 train 권리는 어느 것도 허용으로 올리지 않았다. 10건 중 5건만 store 허용이고 5건은 근거 부족으로 unknown(deny)이다.
- `PinnedTransport`는 httpx 내부 `_pool` 속성을 교체한다. httpx 버전을 올릴 때 `tests/test_collector_fetch.py`·`test_collector_fetch_tls.py`가 깨지는지 먼저 본다.
- 정확·근사 중복은 같은 tenant/scope 안에서만 본다. 문서 본문은 envelope JSONB에 들어 있어(상한 1M자) 코퍼스가 커지면 본문 저장 위치를 blob으로 옮기는 결정이 필요하다.
- 생성물이 지식 도서관으로 가는 어댑터(S2-08), 정책 관리 API/화면(S2-07), 주기 scan(`collector-schedule`), `POST /jobs/{id}/retry`는 아직 없다. `collector-consume`은 기본 off다.
- 배포·운영 DB 적용은 하지 않았다. Alembic 0063은 신규 표 5개만 추가한다(online-safe, 기존 표 변경 없음). 원격 `main`의 동시 추가 `0062_delivery_change_sets` 뒤에 직렬화했다.

## 다음 작업 후보

1. S2-05 예산/캐시 → teacher·judge를 붙이기 전 선행.
2. L1 모델(1순위 epfml/FineWeb2-HQ-PlusPlus-Classifier, 기준선 fineweb-edu-classifier)을 `QualityScorer`로 탑재하고 S3 평가로 판별력을 검증. 27건 표본은 근거로 부족하다.
3. hold 표본 reservoir 감사 절차(회수 가능성), 재처리 job.
4. `/sources` API·화면(S2-07)과 knowledge adapter(S2-08).
