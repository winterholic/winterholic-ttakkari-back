# KB P2 — 배포 후 운영 readback (한정)

작성일: 2026-10-30 (Asia/Seoul)

## 판정

**운영 readback: PASS, 지원 MCP surface로 관찰한 범위에 한정.** Current revision 문서 목록과 search provenance, 한 문서의 historical/current revision 관계, bounded evidence/hash 응답을 운영 owner-scoped API에서 확인했다. **전체 context 숨김이나 전체 복구 완료 판정은 아니다.** 숨김 대상 분모·scope/category별 제외 수가 응답에 없고 mixed-history filter, historical passage, alias direct-ID, research resume 운영 호출은 수행되지 않았다.

## 배포와 기존 증거의 경계

- `backend_deploy_status(job_id="1b79dd88-7bf1-4e01-b3b3-024ba0f8b251")` 직접 응답: `succeeded`, `skip_tests=false`, `revision=activated_revision=48eac0c612d7186e8c418fe607a036ea2ab4da09`, candidate generation `bb450175-d7a4-495e-ae0f-e3202ce0a456`.
- 오케스트레이터 인계로 전달된 root readback(사용자 직접 발화가 아님): 같은 SHA가 runtime `f18d1337` active / `bb450175` epoch 123에서 활성 상태. 함께 전달된 `merge-base --is-ancestor 93fccd72a94138b61bd9d01252be5bf0bc22504c 48eac0c6` exit 0은 누적 배포 ancestry만 확인한다. 이들은 KB 데이터 readback 증거와 분리한다.
- 승인 검증 기록에서 제품 `d3ac880b8c9769070bb0e4e89d5d8974f029ed84`, 테스트 `24d37720101b3d49f50d5ae4816c3880f5c25f84`의 독립 실 PostgreSQL 회귀 `17 passed / 1.86s`를 보존한다. 해당 테스트·initdb는 재실행하지 않았다. 코드/isolated DB 회귀와 운영 API readback은 서로 다른 증거다.
- 읽기 전용 MCP 호출만 했다. 운영 DB 직접 접속, 운영 자료 수정, fixture, 코드·정책 변경, 배포는 없었다. `kb_library_inventory`는 callable surface에 없었다.

## 운영 응답의 분모와 provenance

`kb_documents({limit:100, offset:0})`는 `total=76`, `returned=76`을 반환했다. 현재 목록에서 source kind는 `web=16`, `research_text=60`; 보이는 current revision 번호는 `rev1=68`, `rev2=8`이었다. response에 scope/category, 제외 사유, filter 전 candidate count가 없어 76은 visible library row만을 분모로 한다. hidden rows가 몇 개인지 또는 특정 category가 빠졌는지는 계산할 수 없다.

일반 KB와 개인 맥락 retrieval 구분을 한 질의로 확인했다.

- `kb_documents({query:"winterholic-ttakkari", limit:20, offset:0})` → 0건.
- `kb_search({query:"winterholic-ttakkari", mode:"lexical", limit:5})` → 0건.
- 같은 exact project 이름을 `recall({query:"winterholic-ttakkari af14105d-230f-4ab2-908d-8a1fcae39d00", k:3})`에 전달 → 개인 project category의 백엔드 README 맥락 반환.

이 대비는 개인 프로젝트 자료가 general KB query에서 보이지 않고 별도 recall에 있음을 관찰한 것이다. 0건 두 번만으로 숨김 정책이나 전체 coverage를 증명하지 않으며, 다른 query/hidden source 전체를 평가하지 않았다.

## Live document / evidence probes

1. Exact title `Kubernetes Changed Block Tracking API - Beta Differences`를 `kb_search({query:<title>, mode:"lexical", limit:5})`로 검색했다. Live result의 document ID `73dcea40-3184-4800-b52d-4613cddf1db6`, passage ID `e65e46e3-7bd4-4840-ad54-e79fa103cec4`, current revision ID `1eb05b2a-a288-4e0e-9560-2fa2f37edc8f` (`rev_no=2`)를 사용해 후속 조회했다.
2. `kb_document({document_id:"73dcea40-3184-4800-b52d-4613cddf1db6"})`는 current revision 2와 historical revision 1 (`1b2b6069-1614-4002-b392-d2c7e29be849`)을 함께 반환했다. 두 parsed revision의 `text_sha256`은 `c8171bb4d03edff78c3bb24d2ebe5e887d5d43f413dabce89821ca0bc44e1adb`로 같고, source SHA는 revision 2 `49acb907480d4480e76cfc92f474852723c5d95064007d42cc1685ea9224832d`, revision 1 `21c0eb4ba73421a73f7d886081635c06ff190e595dcd4bcef09f33d875a74e43`로 다르다. 원본 source byte_size는 각각 491326와 490775다. Revision identity와 source identity를 분리해 반환함을 보였다. 목록/search가 historical revision을 포함해 검색했는지는 보이지 않았다.
3. `kb_evidence({passage_id:"e65e46e3-7bd4-4840-ad54-e79fa103cec4", start:0, end:130})`는 passage offset 0–130과 `hash_verified=true`를 반환했다. passage span SHA `8945ef207dbcfd35b8417f44b1a7bf8efed2c83fc299842a29aa8c4b6e6b14b4`는 원본 HTML source SHA와 다르다. 원본 source metadata는 491326 bytes다. 따라서 통과한 것은 저장 passage span hash다. 이 응답은 전체 원본 HTML bytes를 주지 않았다.
4. Live inventory에서 얻은 research reference document ID `b046b2c5-02da-4a02-b45f-9d5d87c2a595`를 `kb_document`로 읽고, 반환 passage ID `219a3561-4377-498e-8593-fb9e6f338edc`를 사용해 `kb_evidence({passage_id:<id>, start:0, end:252})`를 호출했다. source byte_size와 text char_count가 각각 252, span이 0–252, `hash_verified=true`; span SHA, revision `text_sha256`, source SHA 모두 `16af09b77747afa637080629146821fffed8eb913222f1a90cd17ff64335792f`로 일치했다. 반환된 기록 자체는 원 참조 페이지의 API 세부를 읽지 못했다는 unverified follow-up locator다. 일치 해시는 저장 bytes 동일성만 검증하며 사실성은 검증하지 않는다.
5. 두 상세 문서의 alias arrays는 빈 배열이었다. Live alias ID가 제공되지 않았고 historical passage ID도 목록 상세에서 직접 제공되지 않아 alias direct-ID preservation은 호출하지 않았다.

## 남은 운영 검증 범위

- **미검증:** hidden/context 후보를 포함하는 전체 inventory 분모, scope/category별 필터·제외량, `summary` 집계, 두 방향 context↔reference 혼합 revision의 실제 운영 결과, historical context passage의 current_only=false 검색 차단, live covered alias의 direct document/evidence ID 및 vector 경로, research resume 인용.
- 기존 `56d48793db9145894706edf7f38084f4126fce0a` 계열 전체 숨김 FAILED 이력은 삭제·대체하지 않았다. 최신 코드/테스트 증거와 운영 readback은 각각의 범위로 보존한다.
- 필요한 향후 surface는 metadata와 후보 전·후 count를 포함하는 읽기 전용 `kb_library_inventory` 또는 동등한 owner-scoped endpoint다. live alias/historical IDs를 안전하게 찾을 방법, summary 및 research resume의 운영 read-only probe도 필요하다. 현재 surface로 이를 흉내 내기 위해 DB/secret 접근이나 write fixture를 수행하지 않았다.

## 연계 기록

- 승인/실 PostgreSQL 및 세부 후속: [KB P2 독립 재검증 기록](../../../projects/winterholic-ttakkari/back-kb-p2-review-d3ac880/docs/work-history/2026-10-29-kb-p2-independent-reverification.md).
- 통합 checkout main 대조 기록: [KB P2 main integration readback](../../../projects/winterholic-ttakkari/back-kb-p2-integration-20261029/docs/work-history/2026-10-29-kb-p2-main-integration-readback.md).
