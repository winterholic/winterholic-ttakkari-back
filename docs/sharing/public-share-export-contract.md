# 공개 조회·내보내기 계약 v1 — 회의록 첫 수직 기능

상태: **핵심 권한·회수 경합 및 TXT/MD/JSON/CSV formatter·owner export·public 생성 다운로드 구현, 미배포**. public UI와 SRT/VTT/DOCX/PDF는 미구현이며 공개 기능 기본 off다. 실제 구현·검증 범위는 §12–13, 나머지는 후속 목표 계약이다. [ADR 0030](../adr/0030-public-capability-shares-and-exports.md)의 단일 선택안을 구체화한다. 모델/effort 변경이나 후속 작업자 생성은 하지 않는다. 후속 구현자가 볼 공식 계약이며 별도 handoff 문서가 아니다.

## 1. 현재 실체와 근거

조사 기준 HEAD `4a07739cc6bdba75ef8d4b2877c91ea3fabf9f84`. 행 번호는 조사 시점 작업 트리 기준이다. 공통 설정·엔진 파일의 타 세션 미커밋 변경은 이 설계에서 수정하지 않았다.

| 경계 | 현재 코드·문서 | 설계에 주는 제약 |
|---|---|---|
| owner 인증 | [`security/auth.py:25`](../../src/ttakkari/security/auth.py#L25), `:54` — API JWT aud=api, sub=owner 발급; `User=Depends(require_user)` | 새 owner 관리 API에도 User를 필수로 둔다. 새 익명 인증을 require_user에 섞지 않는다 |
| 미들웨어/라우팅 | [`main.py:156`](../../src/ttakkari/main.py#L156) — CORS와 transfer 진단, router 등록. 전역 인증 미들웨어는 없음 | 공개 prefix를 열기 위해 전체 인증 정책을 완화할 필요가 없다. 각 새 route의 의존성을 검토한다 |
| 응답·오류 | `main.py:167–184` — HTTPException은 `{code,message}`, 검증 오류는 errors 추가 | 계약도 이 형태. 익명 인증 오류는 구분 없는404. 오류 본문에 토큰·원본 ID·경로 금지 |
| 회의 GET의 부수효과 | [`api/meetings.py:91`](../../src/ttakkari/api/meetings.py#L91) `_fresh`가 reconcile 실행 | 공개 조회/preview/export에서 owner HTTP handler나 `_fresh`를 호출하지 않는다. 모델·분석·기억 승인·색인 예약 없음 |
| private 회의 DTO | [`meetings/read.py:50`](../../src/ttakkari/meetings/read.py#L50), `:91` — audio, 원본/파생 회의, 분석 Run/session, vector, proposals, analysis_context 포함 | `meeting_detail`을 복사한 뒤 일부 키를 지우는 방식 금지. 공개 allowlist를 새로 조립 |
| 채택 전사 | [`local_stt/read_model.py:15`](../../src/ttakkari/meetings/local_stt/read_model.py#L15) — 같은 회의의 succeeded 전체 job·본문 일치, provider 포인터 우선, child 제외 | 최신 job이 아니라 이 선택 결과를 정규화 원본으로 사용 |
| 발화·표시명 | `read_model.py:41,60`, [`api/meetings.py:231`](../../src/ttakkari/api/meetings.py#L231), [`utterances.py:60`](../../src/ttakkari/meetings/local_stt/utterances.py#L60) | 발화 ID는 job 안의 문자열 순번이다. 원본 ID는 job ID와 묶어 식별. speaker_name은 metadata의 별도 이름 매핑이며 인물 식별 정답이 아님 |
| 분석 근거 | [`meetings/read.py:32`](../../src/ttakkari/meetings/read.py#L32), [`meetings/evidence.py:33`](../../src/ttakkari/meetings/evidence.py#L33) | 채택 발화에 규칙 매칭한 evidence_start/id 재사용. 추정 시각·추가 LLM 금지 |
| 회의 삭제 | [`api/meetings.py:449`](../../src/ttakkari/api/meetings.py#L449) — 회의 삭제, 원음·이미 승인한 기억은 유지 | 공유가 원음/기억을 별도 획득하면 회의 삭제만으로 막히지 않는다. 그래서 자동 연결 금지 |
| PMS/WBS | [`api/pms.py:186`](../../src/ttakkari/api/pms.py#L186), `:211`; [`pms/service.py:314`](../../src/ttakkari/pms/service.py#L314), [`models.py:642`](../../src/ttakkari/models.py#L642) | owner는 User로 접근. WBS/칸반은 같은 PmsTask. 기존 프로젝트 집계는 전체 작업을 읽으므로 공개 부분집계에 그대로 사용 금지 |
| 회사 agent 제한 | [`api/agent_pms.py:65`](../../src/ttakkari/api/agent_pms.py#L65) — 자식 ID도 소유 project로 scope 검사 | 이 권한을 익명 또는 owner share 발급 권한으로 바꾸지 않는다. AgentSession용 share 관리 API는 만들지 않음 |
| 자료실 구분 | [`api/artifacts.py:62`](../../src/ttakkari/api/artifacts.py#L62); [`api/library_insight.py:32`](../../src/ttakkari/api/library_insight.py#L32) | 여기서 자료실은 Artifact 보관함의 선택 파일이다. 내부 맥락 도서관의 검색/출처/graph 공개는 범위 밖. front ResourcesPage는 서버 리소스 화면이므로 자료실로 오인하지 않음 |
| 기존 파일 공유 | [`api/agent.py:201`](../../src/ttakkari/api/agent.py#L201), [`orchestrator/mcp_server.py:126`](../../src/ttakkari/orchestrator/mcp_server.py#L126) | share_file은 권한 폴더 파일을 개인 Artifact로 등록하는 도구. anonymous 페이지 발급이 아님 |
| 기존 익명 파일 링크 | [`security/auth.py:33`](../../src/ttakkari/security/auth.py#L33), [`api/artifacts.py:333`](../../src/ttakkari/api/artifacts.py#L333) — owner가 다운로드 JWT를 발급, `/dl`·`/view`는 User 없이 검증 | 이 경로를 새 share 다운로드의 하위 단계로 사용하지 않는다. 새 share revoke는 기존에 독립 발급한 legacy JWT를 회수하지 못함 |
| 파일 반출/존속 | [`api/artifacts.py:362`](../../src/ttakkari/api/artifacts.py#L362) 정책 재검사; [`api/deps.py:42`](../../src/ttakkari/api/deps.py#L42) deleted 차단; [`artifacts/service.py:142`](../../src/ttakkari/artifacts/service.py#L142) 경로 재검사 | 파일 공유 확장 시 allow·workspace 정책·민감 경로·보존기간·해시·존속을 다시 검사. 현재 helper만으로 보존기간/내용 동일성까지 보장한다고 간주하지 않음 |
| 파일 분류 | [`models.py:279`](../../src/ttakkari/models.py#L279), [`artifacts/service.py:84`](../../src/ttakkari/artifacts/service.py#L84) | Artifact에는 scope 필드가 없음. export_policy allow는 public 동의가 아님 |
| 기존 export | [ADR0012 §결정9](../adr/0012-pms-wbs.md), front `src/features/pms/export.ts:28–66` / `pages/WbsPage.tsx:56` | WBS CSV·xlsx는 브라우저 생성. CSV 수식 시작 문자 방어가 있음. 회의록 서버 export/share route와 DOCX/PDF 생성 의존성은 조사 경로에 없음(pyproject.toml:6–28) |
| 프론트 인증/PWA | front `src/App.tsx:44`, `src/shell/AppShell.tsx:105`, `src/main.tsx`, `vite.config.ts:26–44` | AppShell 로그인 리다이렉트, AuthProvider 전역. 공개 entry는 이 초기화 밖에서 동작해야 함. 기존 API NetworkOnly를 유지하며 share 데이터/SW 캐시 금지. 프론트 수정은 후속 UX 범위 |
| 요청 진단 | [`transfer/diagnostics.py:30`](../../src/ttakkari/transfer/diagnostics.py#L30) | 기존 진단은 raw upload만 대상으로 한다. 새 공개 경로의 헤더/비밀 마스킹은 별도로 확인해야 함 |

최근 기록: [STT UI API](../work-history/2026-10-15-stt-ui-api.md), [재전사 API](../work-history/2026-10-15-retranscribe-api.md), [운영 복구](../work-history/2026-10-15-stt-quality.md). 운영 복구 개인 산출물은 이전 결과함의 `stt-recovery-result.md`로 확인했고, 개인 본문/원음은 이 계약에 복사하지 않았다.

## 2. 권한 불변식과 공개 범위

- 기존 owner 로그인은 개인 본체 전체를 위한 단일 사용자 인증이다. 새 사용자/조직/role 계층을 추가하지 않는다. 새 owner endpoint만 User 사용; public endpoint는 ShareAccess 전용. 로그인 owner라도 capability 없는 public 요청은404다.
- `personal/work/shared`는 출처 분류다. `shared`를 anonymous와 동의어로 쓰지 않는다. root가 work이면 owner 입력 `confirm_work_scope=true`가 필수다. 개인/회사 agent token·transfer token에는 발급/재게시/회수 권한을 주지 않는다.
- 공개 단위는 **root 한 개 + 게시 당시 명시적으로 선택한 sections/IDs**다. root의 상위·이웃·참조 URL·첨부·원음·미래 자식에 권한을 전파하지 않는다. section 선택은 텍스트의 의미상 비밀을 자동 삭제하는 기능이 아니다.
- 조회자는 링크 소지자 누구나이며 재공유가 가능하다. 다운로드를 꺼도 조회한 텍스트의 복사/저장을 막을 수 없다. 다운로드 권한은 서버 생성 형식의 허용 범위다.
- owner export와 anonymous export는 동일한 정규화/투영/formatter를 쓰되 인증 입력과 데이터 원본이 다르다. owner는 요청 때의 원본, anonymous는 승인한 snapshot만 사용한다. anonymous formatter가 원본 DB를 다시 읽어 빠진 내용을 채우면 안 된다.

### 자원별 허용 단위 (회의록만 F1 활성화)

| resource.type / resource.id | 허용할 공개 데이터 | 명시 선택·매 요청 존속 검사 | 자동 공개 금지 |
|---|---|---|---|
| meeting / Meeting UUID | 아래 MeetingDocumentV1의 선택 section | meeting + 채택 job(있을 때) + 선택한 분석 item ID. 원본 scope 고정 | 원음 Artifact, 다른 job/후보, source/derived 회의, 프로젝트 상세, 제안/기억/Run |
| pms_project / PmsProject UUID — 후속 | name, description, status, dates; 선택 task/milestone | `selected_task_ids`, `selected_milestone_ids`의 정확한 현재 목록, 같은 project 관계. members/담당자는 include_people opt-in | notes/리스크/메모, 연결 회의, 멤버 계정, 전체 project dashboard, 미래 작업 |
| wbs / PmsProject UUID — 후속 | 선택 작업의 title, description, status, progress, dates, public_wbs, 선택 집합 안 predecessor refs | selected_task_ids 필수. 조상/자손도 보여 주려면 각각 ID 선택. 미선택 조상 없이 선택 노드를 공개 root로 배치하고 공개 번호 재계산 | 숨긴 조상 이름/형제/전체 수·전체 진행률. PMS/WBS가 같은 DB여도 다른 grant 권한은 합치지 않음 |
| artifact / Artifact UUID — 후속 | 안전한 표시 filename, 허용 MIME, bytes, 선택한 preview 또는 original 하나의 download descriptor | 명시 variant 한 개, pinned bytes hash. 현재 export_policy=allow, Workspace.export_allowed(연결 시), 민감 경로 아님, deleted 아님, retention 유효, 원본 경로/내용 유효 | 폴더 전체, 같은 세션 파일, filesystem path, 기존 signed URL, HTML/스크립트 inline 실행 |

Artifact scope는 연결 세션의 pool로 결정할 수 있으면 이를 고정하고, 세션 없는 파일은 owner가 personal/work를 명시해야 한다. unknown을 personal로 추정하지 않는다. work 확인은 반출 deny/sensitive를 덮어쓰지 못한다. 맥락 도서관 CtxDocument/CtxMemory/CtxSource 공개는 미지원이며 artifact로 변환해 전체 검색을 노출하지 않는다.

부분 프로젝트의 집계는 선택한 작업만으로 다시 계산하고 `aggregation_scope="selected_only"`를 붙인다. 선택 밖 predecessor는 제외하며 숨긴 작업의 ID/이름/개수를 돌려주지 않는다. 프로젝트 링크 revoke는 별도로 명시 발급한 WBS/파일 링크까지 회수하지 않는다. owner의 링크 목록에서 각각 관리하며 root 프로젝트 자체 삭제는 관련 root/선택 binding 검사로 모두 차단한다.

## 3. 발급·회수·snapshot 저장

### 최소 새 모델: PublicShare 한 테이블

일반 ACL/링크 계층/비동기 export-job 테이블은 만들지 않는다. 구체 migration 번호는 구현 시 현재 alembic head를 확인해 부여한다.

| 필드 | 타입/제약 |
|---|---|
| id | uuid PK, 공개 share_id; 자원 ID와 별개 |
| resource_type / resource_id | 검증된 enum + UUID. F1은 meeting만. generic FK가 없으므로 매 요청 root 존재 확인을 생략할 수 없음 |
| created_by | `owner` 고정 문자열; 새 tenant/user FK 없음 |
| secret_hash | SHA-256 hex64, raw 비밀/URL 저장 금지. revoked면 null |
| state / revoked_at / revoked_reason | active 또는 revoked, UTC datetime nullable, reason은 manual/expired/source_unavailable/null. owner effective_state는 reason·기한·존속으로 계산 |
| source_scope | 게시 당시 root의 personal/work. scope가 달라지면 unavailable |
| selection / download_formats | 엄격 schema JSONB. 와일드카드·arbitrary field path 금지 |
| publication_revision | 정수1부터 증가, 재게시 시+1 |
| source_revision | 아래 canonical 후보 hash. owner 변경 충돌 검사에만 사용 |
| snapshot_schema_version / snapshot | 1 / 공개 allowlist DTO만 저장한 JSONB. private owner read model 저장 금지 |
| source_bindings | 비공개 JSONB의 유한 목록 `{type,id,parent_id,role}`. F1은 meeting/job/selected item만. 자식의 부모 관계도 검증 |
| created_at / published_at / expires_at | UTC. 기본7일, 최대90일, 무기한 null 금지 |

active인 `(resource_type, resource_id)`는 첫 버전에서 한 개만 허용하는 partial unique index. expired active 행은 owner의 새 발급 트랜잭션에서 revoke한 뒤 새 ID/비밀을 만든다. 동시 발급 unique 충돌은 owner409다.

비밀 생성은 표준 `secrets.token_urlsafe(32)`에 `ps1_` prefix를 붙인다(256비트 난수). 길이·문자셋을 제한하고 hash 비교는 `hmac.compare_digest`를 사용한다. 비밀번호 hash/새 암호 알고리즘을 만들지 않는다. 유효기간·회수·허용 형식을 비밀 자체의 claim으로 신뢰하지 않는다.

**끄기/폐기**는 같은 DELETE revoke다. state=revoked와 reason을 기록하고 hash=null, snapshot=null, bindings=[]로 같은 DB 트랜잭션에서 지운다. 메타데이터/감사 기록만 남긴다. 만료·고아 source의 payload/hash/bindings도 기존 main._retention_loop의 별도 cleanup 호출에서 제거한다(현재3600초 주기). 서버 정지/정리 오류 시 지연될 수 있어 저장 데이터 제거의 엄격한 시간 상한으로 약속하지 않는다. 정리 지연/실패와 관계없이 매 요청 gate는 즉시 차단하며, 실패는 count/error만 기록하고 조용히 유효한 공유로 취급하지 않는다. 다시 켜면 새 ID·새 비밀이다. 기존 링크를 되살리는 PATCH enabled=true는 제공하지 않는다. 원본 삭제/범위 변경으로 unavailable인 snapshot도 이후 요청에서 서비스하지 않는다. 해당 원본의 삭제/범위 변경 handler는 같은 트랜잭션에서 영향을 받는 share를 revoke하여 payload도 제거한다. 매 요청 gate가 이 hook 누락/동시성에도 fail closed하는 최종 방어다.

기본 미리보기의 revision은 canonical UTF-8 JSON(`sort_keys`, compact separators, `allow_nan=False`)으로 `{선택된 DTO, private bindings, selection, source scope}`를 SHA-256한 값이다. 생성 시각/임의 ID/기본 만료시각은 hash에서 제외한다. source hash와 publication_revision은 서로 다르다. 게시/재게시 시 같은 DB의 REPEATABLE READ snapshot에서 후보를 재계산하고 기대 hash가 다르면409, 아무것도 게시하지 않는다. 원본 source scope와 관계를 재검사한다. 내부 helper가 commit하는 owner HTTP handler를 호출하지 않는다.

## 4. owner 관리·내보내기 endpoint

전부 **User 필수**. 예시는 가상 데이터이며 실제 링크나 자원을 발급하지 않았다. 새 입력 모델은 extra=forbid, bool/int strict, 중복 section 제거 대신422, collection 최대 길이를 제한한다.

| method/path | 요청·응답 |
|---|---|
| POST `/api/shares/preview` | 아래 resource/selection/download_formats/confirm_work_scope. 200 `{document,source_revision,format_options,warnings}`. DB 쓰기·비밀 생성·모델 실행 없음 |
| POST `/api/shares` | 같은 선택 + expected_source_revision + expires_at(생략7일). 201 OwnerShare + **한 번만** capability_url. root/selection revision 재검사 후 저장 |
| GET `/api/shares?resource_type=meeting&resource_id=<uuid>` | owner 링크 metadata 목록, 최대100개/offset. 본문·비밀·URL fragment 없음. query는 명시 enum, unknown422 |
| GET `/api/shares/{sid}` | 200 OwnerShare, 비밀 없음. unavailable/expired/revoked도 owner에는 구분해서 표시 |
| PUT `/api/shares/{sid}/publication` | 완전 교체 선택 + expected_source_revision + expected_publication_revision + expires_at + confirm_work_scope. effective_state=active인 링크만. 200 OwnerShare, revision+1. 동일 비밀 유지; owner가 승인한 새 내용이 다음 요청부터 보임 |
| DELETE `/api/shares/{sid}` | 기존 링크는 반복 호출204. owner가 관리하는 없는 ID404. 후속 public 조회·생성 다운로드 모두404 |
| POST `/api/exports` | `{resource,selection,format,part,expected_source_revision}`. 공개 work 확인은 개인 owner export에 요구하지 않으며, 적용 가능한 기존 반출 정책은 유지한다. 승인한 현재 후보와 일치할 때 생성 바이트200. share 발급/Artifact 생성 없음. source가 변했으면409 |

설정 `public_sharing_enabled=false`가 기본이다. 꺼져 있으면 public route는404, owner 발급/재게시도409 `sharing_disabled`이며 preview/개인 export는 User 인증으로 사용할 수 있다. 후속 테스트만 명시적으로 켠다. 실제 공개 entry/캐시/로그 헤더 검증이 끝날 때까지 운영에서 켜지 않는다.

첫 preview/발급 예시:

```json
{
  "resource": {"type": "meeting", "id": "11111111-1111-4111-8111-111111111111"},
  "selection": {
    "sections": ["summary", "decisions", "actions", "transcript"],
    "include_people": false,
    "include_evidence": true
  },
  "download_formats": ["txt", "md", "json", "csv"],
  "confirm_work_scope": false
}
```

- resource.type의 PMS/WBS/Artifact는 계약상 예약값이며 F1에서는422 `resource_not_enabled`다.
- sections enum: summary/decisions/actions/discussions/open_issues/transcript. 공집합 불가. 기본 자동 선택 없음. title·held_at·analysis_state는 공통 기본이다.
- resource.id/sid는 UUID 문자열, expected_source_revision은 `sha256:`+64자리 hex, expected_publication_revision은 strict int>=1이다. sections 최대6개, download_formats 최대8개(빈 배열은 서버 생성 다운로드 끄기), 중복/알 수 없는 값은422. F1 허용 형식은 txt/md/json/csv이며 대소문자 별칭은 받지 않는다. expires_at은 timezone 있는 미래 ISO8601, 최대 발급/재게시 시점+90일이다. null/과거는422.
- include_people 기본false: participants=[], speaker_name=null, action owner=null. 본문 안 이름을 자동 익명화한다는 약속은 하지 않는다.
- include_evidence 기본false. true이면 transcript section도 선택해야 한다. 그렇지 않으면422 `evidence_requires_transcript`.
- 발급에는 preview의 expected_source_revision 문자열을 추가한다. 임의 본문을 snapshot으로 주입하는 document/payload/path/url 필드는 받지 않는다.

OwnerShare + 발급 전용 필드 예시:

```json
{
  "id": "22222222-2222-4222-8222-222222222222",
  "resource": {"type": "meeting", "id": "11111111-1111-4111-8111-111111111111"},
  "state": "active",
  "effective_state": "active",
  "publication_revision": 1,
  "source_revision": "sha256:<preview-returned-hash>",
  "selection": {"sections": ["summary", "decisions", "actions", "transcript"], "include_people": false, "include_evidence": true},
  "download_formats": ["txt", "md", "json", "csv"],
  "published_at": "2026-10-15T15:00:00Z",
  "expires_at": "2026-10-22T15:00:00Z",
  "revoked_at": null,
  "revoked_reason": null,
  "can_recover_link": false,
  "capability_url": "https://ttakkari.winterholic.net/share/22222222-2222-4222-8222-222222222222#key=<REDACTED_NOT_A_TOKEN>"
}
```

GET/PUT 응답에는 capability_url이 없다. 발급 응답을 잃어도 평문 비밀을 재조회하지 않는다. owner가 기존 링크를 revoke하고 새로 발급한다. OwnerShare.state는 active/revoked, effective_state는 active/expired/unavailable/revoked다. 정리 후에도 revoked_reason으로 expired/unavailable을 구분하고 manual revoke는 revoked로 표시한다. 만료 연장은 effective_state=active에서만 가능하며 상한90일이다. expired/unavailable/revoked의 복구는409 `new_link_required`다.

owner export 예시:

```json
{
  "resource": {"type": "meeting", "id": "11111111-1111-4111-8111-111111111111"},
  "selection": {"sections": ["transcript"], "include_people": false, "include_evidence": false},
  "format": "csv",
  "part": "utterances",
  "expected_source_revision": "sha256:<preview-returned-hash>"
}
```

## 5. public endpoint와 capability 전달

후속 프론트 public entry `/share/{sid}#key=ps1_<secret>`는 AppShell/AuthProvider 초기화 밖에서 동작한다. 기존 owner 로그인 동작은 그대로 둔다. JS는 fragment만 파싱해 아래 헤더로 호출한다. `credentials:"omit"`, owner API client의 자동 JWT/refresh/401-login 처리를 사용하지 않는다. fragment는 새로고침/재방문을 위해 URL에 유지하며 localStorage/sessionStorage/cookie에는 복사하지 않는다. 이력·클립보드·페이지 JS가 볼 수 있다는 한계를 명시한다.

```http
Authorization: Bearer ps1_<opaque-secret>
```

| method/path | 인증 후 응답 |
|---|---|
| GET `/api/public/shares/{sid}` | 200 PublicShareDocument. snapshot만, 원본 관계를 펼치지 않음 |
| HEAD `/api/public/shares/{sid}` | 같은 gate 후200, 본문 없음. auth/존속 검사 생략 금지 |
| GET `/api/public/shares/{sid}/download?format=md&part=document&revision=1` | 같은 gate + 허용 형식/part + revision 확인, 생성 바이트200. JWT/공개 파일 URL/redirect 반환 금지 |
| HEAD `/api/public/shares/{sid}/download?...` | 같은 gate·형식·revision·적용 가능성 확인 후200. 생성 바이트/Content-Length 생략 가능 |

download query의 format과 revision은 필수다. revision은 int>=1, part는 document/utterances/decisions/actions/discussions/open_issues/captions 중 하나다. 생략 part는 document이며 CSV에는 맞는 section part를 명시해야 한다. 모르는/중복 query는 인증 후422다. TXT/MD/JSON은 document만, CSV는 선택한 목록 section만 허용한다. 미선택 part는403 download_not_allowed, 선택했지만 데이터 특성에 맞지 않으면409 format_not_applicable이다.

public에는 목록/검색/자식 탐색/첨부 ID 임의 조회/기존 route proxy/POST 분석/PUT 편집/삭제 endpoint를 만들지 않는다. OPTIONS는 기존 CORS preflight만 처리한다. 기존 CORS에는 HEAD가 허용 메서드에 없으므로 구현 시 HEAD를 정확히 추가하고 Retry-After를 expose_headers에 추가한다. 기존 private route의 User는 유지한다. CORS origin 목록을 `*`로 넓히지 않는다. CORS는 권한 수단이 아니며 header 비밀이 유효하면 CLI 같은 비브라우저도 접근할 수 있다.

유효하지 않은 ID·비밀 누락/잘못됨·다른 share의 비밀·만료·회수·원본/선택 binding 삭제·scope 변경은 모두 동일404다. owner JWT나 legacy download JWT도 share 비밀로 인정하지 않는다. **먼저 capability gate, 그다음 format/part/revision 등 상세 입력 검증** 순서다. sid는 bounded string으로 받고 dependency 안에서 UUID 파싱하여 잘못된 ID를422로 구분하지 않는다. 이 gate 이전에는 resource별 정보를 포함한 validation error를 내보내지 않는다.

다운로드는 fetch의 Authorization 헤더로 받은 Blob을 임시 blob URL로 저장하고 해제한다. 일반 `<a href=/api/dl/...>` 방식으로 폴백하지 않는다. 브라우저 메모리 사용을 제한하기 위해 F1은 snapshot4MiB·발화10,000개·분석항목5,000개 이하, 생성 파일8MiB 이하로 제한한다. 초과는413 `export_too_large`; 조용한 truncation 없음. 초기 생성은 표준 라이브러리만, 동시에2개/프로세스까지 허용하고 포화는503 `export_busy` + Retry-After다. 유효 capability 기준 다운로드10회/분/share, 페이지120회/분/share, IP의 인증 실패30회/분(상한 있는 메모리 bucket)에서429 + Retry-After. 단일 프로세스 구현 목표이며 서버 재시작 시 limit state가 초기화됨을 제한으로 명시한다.

## 6. gate, 폐기와 삭제 이후 의미

모든 public 응답(HEAD/오류/다운로드 포함)은 다음 gate를 거친다.

1. bounded ID/헤더 형식 검증 → share 조회 → 비밀 hash constant-time 비교. 실패는404. 잘못된 키 bucket은 share 존재 여부와 무관하게 적용한다.
2. active, expires_at > DB 현재 시각, snapshot/schema 지원 여부 확인. 장애/알 수 없는 state는 fail closed(503에는 내용/존재 상세 없음).
3. root 존재와 현재 scope==source_scope, source_bindings의 존재·같은 부모·공개 정책 확인. F1: meeting/job/item. 원음은 binding에 넣거나 읽지 않는다.
4. public payload는 저장된 DTO를 strict schema로 다시 검증한다. renderer는 이 DTO 외 데이터 소스를 받지 않는다. 생성 다운로드는 revision과 format/part 권한 확인 후 생성한다.
5. 파일 생성은 DB lock 밖에서 수행하고, 응답 직전에 gate와 revision을 다시 검사한다. final gate는 root → 선택 job/item binding → share 순으로 짧은 `FOR SHARE`를 잡고 DB `clock_timestamp()`로 잠금 대기 뒤 만료를 확인하여 revoke/삭제/범위 변경과 순서를 정한다. 이 검사가 인가의 선형화 지점이다. 원본을 다시 투영하지 않는다.

회수 커밋 뒤 새로 최종 인가되는 응답은 모두 차단한다. 이미 최종 인가된 전송·수신자의 복사본은 완료/보유될 수 있다. 긴 다운로드 전체에 DB lock을 잡아 즉시 폐기를 가장하지 않는다. 생성 중 revoke/revision 변경이면 생성 바이트를 버리고404/409, 임시 파일은 정리한다. F1은 bytes 메모리 응답이라 share와 독립적인 다운로드 ticket/URL/영속 cache가 없다.

binding 하나라도 유효하지 않으면 해당 게시 snapshot 전체가404다. 자식을 뺀 새 모습으로 조용히 축소해 반환하지 않는다. 재분석이 이전 분석 item을 삭제·교체한 경우도 unavailable/revoked다. 이런 링크는 같은 비밀로 되살리지 않고 owner가 revoke 후 새 링크를 발급한다. 일반 텍스트 편집처럼 binding이 유지되는 경우만 PUT 재게시를 허용한다. 내용 편집/화자 이름 편집은 snapshot을 바꾸지 않지만 source scope 변경은 차단한다. 선택한 텍스트 자원과 원음 파일의 권한은 별개이며, 회의 공유가 원음 export_policy를 바꾸거나 파일 바이트 접근을 허용하지 않는다. 자연어 본문 전체의 정보등급을 추론하는 별도 정책은 현재 데이터 모델에 없으므로 owner의 내용 확인이 남는다. 회의를 재전사해 파생 회의가 생겨도 새 회의가 자동 공유되지 않는다. 원본 삭제로 남은 원음/승인 기억은 이 grant의 별도 권한이 아니므로 접근 불가다.

legacy `/api/dl`로 이미 발급한 파일 링크나 독립 생성한 다른 share는 이 revoke 대상이 아니다. 새 공개 데이터에 그 링크를 포함하지 않고 새 public download도 이를 발급하지 않는다. 이 한계를 “모든 과거 다운로드까지 즉시 회수”라고 표현하지 않는다.

## 7. 공개 DTO 허용목록과 회의록 정규화

### 정규화 원본과 공개 투영 순서

`build_meeting_document(db, meeting_id)`는 owner/공개 게시 후보 공용의 **순수 읽기** 서비스다. stable 상태 transcribed/analyzed만 F1에서 허용하고 나머지는409 `source_not_ready`. `_fresh`/reconcile/start_analysis/LLM/모델/기억 승인을 호출하지 않는다.

1. Meeting + 채택 job(`adopted_job`) + 그 job의 `job_utterances`/speaker_names + MeetingItem을 일관된 DB snapshot에서 읽는다.
2. local_whisper 출처인데 채택 job을 못 찾으면409 `stt_source_unresolved`. 최신 job/child/raw로 대체하지 않는다. job이 명시적으로 quality 차단 상태면409 `quality_review_required`. legacy metadata 부재는 unknown 경고이며 “검증 완료”로 바꾸지 않는다.
3. 붙여넣기/파일 전사는 adopted_stt_job_id=null, utterances=[], timed=false, text에 현재 본문을 둔다. 가짜 화자·시각·발화 ID를 만들지 않는다.
4. 분석 items는 현 채택 발화에 `locate_evidence` 규칙을 적용한다. 결과가 유일하지 않으면 evidence_start/id=null. 시작 시각은 발화 시작이며 단어 단위 정답 시각이 아니다.
5. `project_meeting_document(document, selection)`이 아래 DTO를 **키별로 조립**한다. excluded section·people/evidence 규칙 적용 후 모든 formatter가 이 투영만 받는다. 원본 text/analysis_context를 fallback으로 다시 붙이지 않는다.
6. 인식 가능한 내부 `/api/dl/`, `/api/view/` 서명 링크·새 share capability URL이 자유 텍스트에 있으면 owner preview/게시/export는422 `private_link_in_content`. 링크의 비밀은 오류/로그에 출력하지 않는다. 임의의 자연어 속 모든 비밀을 탐지하는 기능은 아니므로 owner 미리보기 확인은 남는다. 외부 URL을 가져오거나 Markdown 이미지/첨부를 자동 resolve하지 않는다.

### MeetingDocumentV1 필드 — 이 목록 이외 금지

| 경로 | 타입/원본·의미 |
|---|---|
| schema_version / kind | 1 / meeting |
| title / held_at | Meeting.title / ISO8601 timezone 포함 또는 null |
| included_sections | selection.sections, 공개한 section만 나열 |
| section_states | 모든 section의 available / empty / not_generated / not_selected. 제외됨과 미분석을 구별 |
| analysis_state | not_generated 또는 available. transcribed에서는 not_generated. 자동 분석 요청 아님 |
| participants | include_people=true일 때 이름 문자열 목록, 기본[] |
| summary | 선택되고 분석 결과가 있으면 문자열, 그 외 null |
| decisions/discussions/open_issues/actions | 선택된 typed item 목록, 미선택/미분석이면[] |
| 각 item 공통 | item_id(UUID), position(int), text(string), detail(string\|null), status(open/done/cancelled). status는 기억 승인 여부가 아님 |
| actions 추가 | owner(string\|null, include_people 적용), due_text(string\|null). 기존 due는 자유 문자열이므로 날짜로 추정 변환하지 않음 |
| 각 item evidence | evidence_quote(string\|null), evidence_start(number seconds\|null), evidence_utterance_id(string\|null). include_evidence=false면 전부null. true는 transcript 선택 필수 |
| transcript | 미선택이면 null, 선택 시 아래 object |
| transcript.adopted_stt_job_id | 선택된 전체 성공 job UUID 또는 null. 출처 재현용으로 **명시적 공개 허용**. 이 ID만으로 private STT API에 접근할 수 없음 |
| transcript.source / timed / duration_seconds | local_whisper/paste/file, boolean, number\|null. 자동 인식 여부를 보존 |
| transcript.text | 발화가 있으면 선택된 speaker/검증 상태를 적용해 발화 목록에서 다시 렌더링. 없으면 붙여넣기 본문. 이름 매핑이 적용되지 않은 private 렌더 문자열을 덧붙이지 않음 |
| transcript.utterances[] | 아래 원본 발화 ID·시각·화자·본문·검증 정보 |
| utterance_id | 현재 job_utterances의 id 문자열 그대로. composite identity=(adopted_stt_job_id,utterance_id); 다른 job/재전사로 재사용·복사하지 않음 |
| start/end | finite number, 원음 시작 기준 초. 0도 유효. duration 범위 내, start<=end. 정렬은(start,end,원본 순서)이며 ID 재부여 금지 |
| speaker_id / speaker_label | 음향 라벨과 표시 라벨, string\|null. null이면 “화자 미상” |
| speaker_name | 사용자 job별 mapping의 string\|null, include_people=false면null. speaker_id/실명 식별 층과 분리 |
| text / verification_status / overlap | 현재 표시용 발화 text, unverified/needs_review, true/false/null. 알 수 없는 verification 상태는 needs_review로 보수 처리. 사람 검수 완료를 생성하지 않음 |
| transcript.review | unverified 또는 needs_review, review_utterance_count, unknown_speaker_count. transcript 선택 시만 계산, 정답 정확도 점수 아님 |

owner 정규화 객체의 provenance에는 meeting_id·source_scope·선택 item/job binding이 더 있지만 **공개 MeetingDocumentV1에는 없다**. 공개 JSON에서도 flags/segment_positions/words/raw/로그확률/토큰/실행 PID·모델 경로·quality_retries·original_text·전체 metadata를 내보내지 않는다. 공개 root에 source/derived IDs, audio/artifact IDs, project ID/URL, run/session IDs, proposals, conflicts, analysis_context, vector 필드를 추가하지 않는다. job/item UUID와 원래 발화 ID는 위 재현·근거 연결에 필요한 제한된 예외다.

공개 envelope 허용목록: schema_version, share_id, resource_type, publication_revision, published_at, expires_at, included_sections, document, downloads뿐이다. downloads는 허용되고 현재 생성 가능한 `{format,parts}` 목록이며 **URL/secret 없음**. 원본 scope·private bindings·전체 자원 목록·내부 경로·비밀 hash·capability는 포함하지 않는다.

가상 공개 응답(미분석 회의·원문 전사만 선택):

```json
{
  "schema_version": 1,
  "share_id": "22222222-2222-4222-8222-222222222222",
  "resource_type": "meeting",
  "publication_revision": 1,
  "published_at": "2026-10-15T15:00:00Z",
  "expires_at": "2026-10-22T15:00:00Z",
  "included_sections": ["transcript"],
  "document": {
    "schema_version": 1, "kind": "meeting", "title": "공유 예시 회의", "held_at": null,
    "included_sections": ["transcript"],
    "section_states": {"summary":"not_selected","decisions":"not_selected","actions":"not_selected","discussions":"not_selected","open_issues":"not_selected","transcript":"available"},
    "analysis_state": "not_generated", "participants": [], "summary": null,
    "decisions": [], "actions": [], "discussions": [], "open_issues": [],
    "transcript": {
      "adopted_stt_job_id": "33333333-3333-4333-8333-333333333333", "source":"local_whisper", "timed":true, "duration_seconds":30.0,
      "text":"[0.00–2.40] 화자 1 [미검수]: 다음 일정을 확인하겠습니다.",
      "utterances":[{"utterance_id":"0","start":0.0,"end":2.4,"speaker_id":"SPEAKER_00","speaker_label":"화자 1","speaker_name":null,"text":"다음 일정을 확인하겠습니다.","verification_status":"unverified","overlap":null}],
      "review":{"status":"unverified","review_utterance_count":0,"unknown_speaker_count":0}
    }
  },
  "downloads": [{"format":"txt","parts":["document"]},{"format":"json","parts":["document"]}]
}
```

분석 인용은 기존 helper가 반환한 ID와 같은 adopted_stt_job_id의 발화에만 연결한다. transcript 미선택이면 evidence 필드를 구조적으로 null로 만든다. 분석 본문에 자연스럽게 들어 있는 인용까지 자동으로 비공개 처리한다는 의미는 아니다.

## 8. 형식별 계약과 구현 가능 범위

**F1 구현 상태(2026-10-15): TXT/MD/JSON/CSV가 활성화되어 owner/public 생성 다운로드에서 동작한다.** SRT/VTT/DOCX/PDF는 계속 비활성이고 public UI도 미구현이다. preview `format_options`는 `{format,enabled,applicable,parts,reason}`를 돌려주고, public downloads는 grant가 허용한 형식과 현재 DTO에서 생성 가능한 part만 포함한다. 미활성 형식 요청은422 `format_not_enabled`; 선택된 분석이 아직 생성되지 않은 CSV part는409 `analysis_not_generated`, 그 밖의 데이터 부적합은409 `format_not_applicable`이다. 실제 구현의 세부와 검증은 §13을 따른다.

| 형식 / Content-Type | 가능한 대상·part | 구현 수단/준비 상태 | 빈값·미분석/시각 조건 |
|---|---|---|---|
| TXT / text/plain; charset=utf-8 | meeting document; 이후 PMS plain report | **F1 목표**, Python stdlib | title/date/선택 section을 문단으로. 미분석 summary는 “분석되지 않음”, 빈 분석 항목은 “항목 없음”, 미선택 section은 아예 출력하지 않음 |
| MD / text/markdown; charset=utf-8 | meeting document; 이후 PMS/WBS selected report | **F1 목표**, stdlib, 표/헤딩 구문 이스케이프 | TXT와 같은 정보. 사용자 HTML을 실행하지 않고 링크/이미지 자동 임베드 없음 |
| JSON / application/json | MeetingDocumentV1 document; 이후 자원별 typed DTO | **F1 목표**, json/Pydantic | null/[]와 section_states 보존. owner provenance/raw는 export에도 제외. allow_nan=false |
| CSV / text/csv; charset=utf-8 | meeting utterances/decisions/actions/discussions/open_issues 중 하나; 이후 WBS tasks | **F1 목표**, csv/io. 기존 WBS CSV 로직은 참고만 | 한 파일에 이종 표 섞지 않음. 분석 완료된 선택 목록이 비어 있으면 헤더만. 미분석 분석 항목은409 analysis_not_generated. summary 문자열에 CSV를 제공하지 않음. 붙여넣기는 utterances CSV 부적합 |
| SRT / application/x-subrip; charset=utf-8 | meeting captions만 | 후속 조건부, 별도 ML 불필요, stdlib formatter 검증 필요 | 선택 transcript + 실제 timed 발화 필요. 미분석은 무관. untimed/빈 발화/시각 오류는409. 합성 시각/무음 cue 생성 안 함 |
| VTT / text/vtt; charset=utf-8 | meeting captions만 | 후속 조건부, [W3C WebVTT](https://www.w3.org/TR/webvtt1/) 준수 formatter 검증 필요 | SRT 조건과 같음. WEBVTT 헤더, dot 밀리초, cue text escape |
| DOCX / application/vnd.openxmlformats-officedocument.wordprocessingml.document | meeting document; 이후 selected PMS/WBS report | **후속**, [python-docx](https://python-docx.readthedocs.io/en/latest/) 후보, 현재 pyproject 생성 의존성 없음 | 단락/표로 같은 DTO 표현. 미분석 표기 동일. 템플릿·이미지·외부 링크/OLE 자동 삽입 금지. 실제 한글 줄바꿈/파일 열기 검증 전 enabled=false |
| PDF / application/pdf | meeting document; 이후 selected report | **후속**, ReportLab 후보; 로컬 한글 TTF와 임베딩 라이선스·페이지 나눔 검증이 추가 필요 | 같은 DTO로 로컬 생성. 사용자 HTML/URL을 브라우저나 온라인 변환기로 보내지 않음. Paragraph 등에 넣을 문자열도 XML escape하고 외부 이미지/링크 해석을 끈다. 폰트 부재/미검증 시 enabled=false; 빈 사각형 PDF로 성공 처리 금지 |

TXT/MD의 발화는 같은 화자·verification_status·overlap이고 간격2초 이하인 인접 발화를 문단으로 묶어 읽을 수 있게 한다. 문단에 시간 범위와 표시명/라벨·검증 표식을 남긴다. 합치는 것은 출력 표현만이며 원본 ID·JSON/CSV 발화 목록·근거 연결을 바꾸거나 반복 문장을 지우지 않는다. speaker_name이 없으면 speaker_label, 둘 다 없으면 화자 미상. needs_review는 `[검증 필요]`, unverified는 `[미검수]`를 보존한다.

CSV columns는 다음 고정 순서다. 세미콜론/지역별 형식의 추정은 하지 않는다.

- utterances: adopted_stt_job_id,utterance_id,start_seconds,end_seconds,speaker_id,speaker_label,speaker_name,text,verification_status,overlap.
- decisions/discussions/open_issues: item_id,position,text,detail,status,evidence_quote,evidence_start,evidence_utterance_id.
- actions: item_id,position,text,detail,status,owner,due_text,evidence_quote,evidence_start,evidence_utterance_id.

UTF-8 BOM + CRLF, csv writer quoting으로 쉼표/따옴표/줄바꿈을 처리한다. null은 빈 셀, true/false는 그 문자열, 시간0은0으로 보존한다. **텍스트 셀**의 처음 의미 있는 문자가 `= + - @`이거나 처음에 tab/CR/LF가 있으면 apostrophe prefix로 수식 실행을 방어한다. 공백·BOM·제어 문자 뒤 수식 문자도 검사한다. 숫자형 정상값은 문자열 수식으로 취급하지 않는다. JSON/TXT의 실제 내용은 CSV 안전장치 때문에 바꾸지 않는다. 미분석 분석 목록을 빈 결과 CSV로 위장하지 않으며 format_options의 해당 part도 applicable=false/reason=analysis_not_generated다. JSON export의 body는 공개 envelope가 아닌 투영된 MeetingDocumentV1 그대로다. CSV 수식 방어는 [OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection)을 근거로 한다.

후속 caption 규칙: 원음 기준 start/end를 Decimal 반올림(half-up) 밀리초로 변환, SRT `HH:MM:SS,mmm`, VTT `HH:MM:SS.mmm`. 빈 text/비유한/음수/역전/반올림 뒤 길이0인 cue는 생성 전체를409 `caption_timing_invalid`로 거절한다. 오류 cue를 조용히 누락하거나 임의로 늘리지 않는다. overlap은 겹친 cue로 보존하고 화자·검증 표식을 cue text에 포함한다. player별 중첩 표시 한계는 별도 검수 대상이다. pasted transcript의 시각을 LLM으로 만들지 않는다.

생성 filename은 `meeting-<share_id>-r<revision>.<허용 확장자>`(owner export는 `meeting-export.<확장자>`)로 고정한다. 사용자 title·path를 header/경로로 사용하지 않는다. Content-Disposition attachment와 RFC5987 filename* UTF-8, nosniff를 설정한다. Range는 F1에서 미지원(Accept-Ranges:none); capability 확인 뒤 Range 요청은416 `range_not_supported`. source/format 오류를 HTML 로그인 페이지로 바꾸지 않는다.

## 9. 캐시·검색·토큰 노출과 오류

모든 share 관련 성공/오류와 owner의 비밀 발급 응답에 `Cache-Control: private, no-store`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Robots-Tag: noindex, nofollow, noarchive`를 적용한다. public API에는 Vary: Origin, Authorization을 설정하고 ETag/Last-Modified/304 최적화는 F1에서 하지 않는다. 조건부 요청도 gate를 생략하지 않는다.

공개 HTML shell은 데이터·title·OpenGraph 본문 없이 일반 안내만 제공한다. 후속 프론트는 외부 analytics·세션 리플레이·OG fetch·외부 font/CDN·이미지 자동 로드 없이 동작해야 한다. shell CSP는 default-src 'none', script-src/style-src 'self', connect-src는 알려진 API origin, img-src 'none', object-src 'none', base-uri 'none', frame-ancestors 'none'. 실제 배포에서 header 적용과 entry 분리를 검증하기 전 공개 기능을 켜지 않는다. 디자인은 별도 UX backlog 범위다.

새 API/공개 본문은 SW/브라우저 앱 저장소/React Query persistence/CDN 캐시에 저장하지 않는다. 기존 API NetworkOnly는 유지하고, 공개 화면을 벗어나면 메모리 데이터·blob URL을 해제한다. off-line 재열람은 지원하지 않는다. 새 PublicShare.snapshot을 CtxSource/meeting_chunks/recall/사이트 검색·sitemap에 등록하지 않는다. robots 정책은 검색 방지 요청일 뿐 인가가 아니며 수신자의 별도 복사/게시를 막지 못한다.

서버/프록시/CDN/오류 수집에서 Authorization, 발급 응답의 capability_url, body, hash, fragment를 기록하지 않는다. public access log에는 경로 template·status·요청 ID만, audit에는 share ID/actor(owner 또는 anonymous)/action/outcome/형식만 둔다. raw UA/전체 URL/query를 감사 detail에 넣지 않는다. DB 장애 traceback도 capability/본문을 포함하지 않게 한다. URL fragment는 HTTP dereference 전에 분리되지만 페이지 JS와 브라우저 기록에서는 보일 수 있다([RFC3986 §3.5](https://www.rfc-editor.org/rfc/rfc3986.html#section-3.5)). no-referrer는 별도 전송 정책이다([W3C](https://www.w3.org/TR/referrer-policy/)). no-store를 개인정보 유출 방지의 충분조건으로 취급하지 않는다([RFC9111 §5.2.2.5](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5)).

| 상태/상황 | code / 예시 |
|---|---|
| owner JWT 없음/만료 | 401 unauthorized, 기존 WWW-Authenticate 유지 |
| owner work 확인 누락 | 403 work_scope_confirmation_required |
| owner 원본 없음 | 404 not_found |
| owner duplicate active / source 또는 publication revision 다름 | 409 already_shared / source_changed / revision_conflict |
| public ID/비밀/상태/root/binding 실패 전부 | 404 share_unavailable. 동일 메시지·본문, secret 없어도 존재를 알려주지 않음 |
| 유효 capability지만 미허용 download format | 403 download_not_allowed |
| 유효 capability의 revision 불일치 | 409 revision_conflict. 다시 GET하도록 안내, 자동 최신 파일 반환 안 함 |
| 미지원 resource/format·extra field·잘못된 선택 | owner422 resource_not_enabled / format_not_enabled / validation 등 |
| 형식 부적합/원본 미준비/시각 이상 | 409 format_not_applicable / source_not_ready / caption_timing_invalid |
| 제한/DB·인가 장애 | 429 rate_limited / 503 share_unavailable 또는 export_busy. 인가 장애일 때 본문 캐시로 성공시키지 않음 |

```json
{"code":"share_unavailable","message":"공유를 사용할 수 없습니다."}
```

```json
{"code":"revision_conflict","message":"공개 내용이 갱신되었습니다. 다시 조회해 주세요."}
```

```json
{"code":"format_not_enabled","message":"이 형식은 아직 지원하지 않습니다."}
```

## 10. 첫 구현 파일과 반례 — 후속 Luna high의 범위

이 절은 계약의 구현/검증 명세이며 작업자를 생성하거나 자동 위임하는 지시가 아니다. 후속 구현도 frontend를 수정하지 않고 owner/public API와 생성 다운로드까지 한 수직 기능으로 끝낼 수 있다. 실제 anonymous HTML page 공개는 별도 프론트 entry/배포 보안 검증을 통과한 뒤다. 백엔드 API의 익명 공개만으로 UI 페이지 완성이라 부르지 않는다.

| 파일 | F1 변경 책임 |
|---|---|
| `src/ttakkari/meetings/document.py` (신규) | 채택 job·원래 발화 ID·named_utterances·분석 근거의 순수 정규화. 별도 공개 projection. read.meeting_detail 전체 호출/metadata 통째 복사 금지 |
| `src/ttakkari/sharing/schemas.py` (신규) | strict owner 입력, MeetingDocumentV1/PublicShareDocument 허용목록, resource/format/section enum. Public DTO extra=forbid |
| `src/ttakkari/sharing/service.py` (신규) | preview/source hash, grant 발급·재게시·revoke, 유한 binding 검증, 별도 ShareAccess dependency, 공통 final gate |
| `src/ttakkari/sharing/exports.py` | 구현됨: 네 F1 pure formatter. 형식/part 적합성은 공통 wrapper가 검사하고 CSV 수식 방어·byte 한도를 적용한다. URL fetch/LLM 없음 |
| `src/ttakkari/api/shares.py` (신규) | `/api/shares*`, `/api/exports` owner router. 모든 route에 User |
| `src/ttakkari/api/public_shares.py` (신규) | public GET/HEAD/생성 download router, 별도 capability와 generic404; 로그인 리다이렉트/legacy 파일 URL 없음 |
| `src/ttakkari/models.py`, `config.py`, 새 alembic revision | PublicShare 하나와 index, 기본 off인 public_sharing_enabled 설정. 기존 타 세션 수정 위에서 통합, migration 번호 재확인. 이 설계 작업은 파일을 건드리지 않음 |
| `src/ttakkari/main.py` | 새 router 두 개 등록, 공개 경로에도 오류/보안 header가 적용되도록 정확한 ASGI 범위 처리. 기존 User/private router 권한 완화·CORS origin 와일드카드 금지. HEAD/Retry-After만 필요한 CORS 허용표에 추가한다. 기존 retention loop에 만료/고아 snapshot 정리 호출을 추가하되 gate의 정확성을 정리 주기에 의존시키지 않음 |
| `api/meetings.py`의 delete, item 교체 경로 | 삭제/선택 binding 소실 시 grant revoke+payload 제거. public gate를 최종 방어로 유지; 모델 실행/재전사 코드는 변경 불필요 |

반드시 실패부터 재현할 회귀 반례(운영 DB 대신 새 테스트 DB, 가짜 회의·발화/파일 사용):

1. `tests/test_public_share_auth.py`: owner 없는 발급/재게시/삭제401. public capability로 owner/PMS/회의/Artifacts/private STT 접근401. owner/agent/transfer/download JWT로 public 접근404. share A의 비밀로 B 조회/다운로드404. 잘못된 ID/토큰/만료/회수/원본 삭제 모두 같은404. no secret + malformed format에서도 format별 정보가 먼저 나오지 않음.
2. `tests/test_share_lifecycle.py`: source 변경 뒤 stale preview409·DB grant0. 동시 발급은 하나만 성공. 재게시 전 새 이름/새 항목 안 보임. publication revision1 다운로드 중 재게시하면 final gate409. 생성 중 revoke/delete면404·바이트 미전송. revoke idempotent·hash/payload 제거·재발급 old link 불가. scope personal→work 변경/선택 item 삭제/재분석 item 교체도 차단.
3. `tests/test_share_projection.py`: private DTO에 audio/raw/words/tokens/original_text/metadata/PID/path/proposals/conflicts/analysis_context/project/source/derived/run/session/vector 같은 sentinel을 넣어도 public JSON·4개 export 어디에도 안 나옴. root 선택만으로 첨부/부모/파생/후보가 안 나옴. people=false 이름 필드 null/[]; evidence=false quote/id/time 모두null. transcript 미선택이면 evidence=true422이며 summary-only JSON/CSV가 transcript를 읽지 않음. 내부 signed link가 텍스트에 있으면422이고 오류에 비밀이 안 나옴.
4. `tests/test_meeting_document.py`: 최신 queued/child 대신 본문과 일치한 전체 succeeded job 채택. 원래 utterance id와 speaker mapping 보존, job별 이름 분리. 시간0/unknown speaker/overlap null/needs_review 보존. 인용문 중복·미매칭은null. pasted text는 jobnull·발화[]·untimed. 미분석 항목을 LLM으로 채우지 않음. failed/분석중 상태나 source mismatch409.
5. `tests/test_meeting_exports.py`: TXT/MD는 선택 section만 문단으로, JSON allowlist exact shape, CSV null/0/quote/newline/Unicode/BOM·`=cmd`, 공백 뒤 `+cmd`, tab 뒤 `@...` 방어·실제 숫자 유지. 분석 완료된 빈 선택 항목 CSV는 헤더만, 미분석 분석 항목 CSV는409 analysis_not_generated. 잘못된 part/미활성 형식/oversize는 오류, 조용한 누락 없음. title의 CRLF/path/script로 header/경로를 못 바꿈.
6. `tests/test_public_share_delivery.py`: 다운로드 권한 제한이 JSON/TXT의 우회 데이터 확장으로 이어지지 않음. 모든 응답/HEAD/에러에 no-store/no-referrer/noindex/nosniff. 허용 origin의 Authorization+HEAD preflight 성공과 비허용 origin 차단, private JWT 요구 유지도 검사. conditional/Range도 인가 확인. 응답에 dl/view URL·secret 없고 redirect 없음. DB 장애는503·내용 미반환. logs/audit에 주입한 비밀/본문 sentinel 없음. rate limit·concurrent generation limit가 bounded이고 Retry-After 있음.
7. 추후 file 확장 전: deny/sensitive/워크스페이스 반출 금지·보존 만료·삭제·원본 교체·symlink·hash 변경·미선택 variant·cross-grant file ID는 조회/다운로드 차단. pms 확장 전: 다른 project의 task IDs·미선택 조상/형제·전체 집계·미래 추가 task·scope 변경이 새어 나오지 않음. 이 조건 전에는 type 활성화 금지.

F1 완료 기준은 migration round-trip, 관련 반례, 격리 DB 전체 pytest, 소스 허용목록/실제 응답 검사다. 외부 공개 점검은 **가상 자원**으로 하고 실제 회의 링크나 원음·전사를 테스트 서버/변환 SaaS로 보내지 않는다. DOCX/PDF/자막·프론트/디자인·PMS 확장을 F1의 숨은 완료 조건으로 만들지 않는다.

## 11. 설계 세션 조사/검증 상태 (구현 전 이력)

이 문단은 2026-10-15 설계 세션 당시의 이력이다. 당시에는 코드 경로·문서와 공식 자료를 읽고 ADR/계약만 작성했으며 pytest/보안 동작 검증/실제 HTTP 공유 시연은 **미실행: 공유 구현을 하지 않았기 때문**이었다. 후속 코드 상태는 §12–13을 기준으로 한다. 이전 STT의671 passed를 export 기능 테스트 근거로 재사용하지 않는다.

조사 당시 운영은 d61e3a0·PID61369였고 이전 STT 복구 데이터는 운영 저장 완료 상태의 기록을 확인했다. 그 설계 시점에는 새 공유 기능이 미구현·미배포였다. 이후 변경의 현재 배포 여부는 따로 확인하지 않았다. 공개 링크/운영 DB 변경/모델 실행/배포/재시작/프론트 수정/자동 후속 위임은 하지 않았다.

공식 확인 자료: [RFC3986 fragment](https://www.rfc-editor.org/rfc/rfc3986.html#section-3.5), [RFC9111 no-store](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5), [W3C Referrer Policy](https://www.w3.org/TR/referrer-policy/), [OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection), [WebVTT](https://www.w3.org/TR/webvtt1/), [python-docx](https://python-docx.readthedocs.io/en/latest/), [ReportLab TrueType](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/). 2026-10-15에 읽었다. 사용자 제공 Notta 참고 이미지는 web fetch가 cache miss여서 실제 화면 내용은 확인하지 못했으며, 이 계약은 그 UI를 분석하거나 복제하지 않는다.


## 12. 2026-10-15 핵심 권한·회수 구현 상태

- 구현: `meetings/document.py`의 선택 필드 직접 조회·공개 allowlist 조립, `sharing/schemas.py` strict DTO, `PublicShare`/migration `0028`, owner preview/create/list/get/republish/revoke, public GET/HEAD와 공통 최종 응답 인가. 기존 owner 회의 handler/reconcile은 호출하지 않는다. 기존 owner JWT 검증에 `sub=owner`도 요구한다.
- 이 문단은 core 구현 완료 당시의 상태다. 그 시점에는 `sharing/delivery.py::FORMATTERS`가 비어 있고 `/api/exports`와 실제 formatter가 미구현이었다. 후속 상태는 §13 및 [export 후속 작업기록](../work-history/2026-10-15-public-share-exports.md)을 따른다.
- 후속 formatter 계약: `async (MeetingDocumentV1, part: str) -> bytes`. wrapper가 strict 승인 DTO의 deep copy와 검증한 part만 넘긴다. DB·원본 ID·capability·파일 경로·legacy ticket을 받거나 원본을 다시 읽어 채우지 않는다. grant 형식 권한과 part 선택/적합성, revision, 8MiB, 2개 동시 생성, 10회/분/share를 공통 wrapper가 검사한다. 생성은 lock 밖, `AuthorizedResponse.__call__`이 `http.response.start` 직전 공통 `final_gate`를 실행한다. 자체 Response/URL/ticket 우회는 금지한다. 형식별 escape/CSV 수식 방어·품질 검증을 마친 뒤 해당 formatter만 등록한다.
- 새 DB trigger가 root 삭제/scope 변경, 선택 item 삭제/부모·kind 변경, 선택 job 삭제/부모·status 변경 시 같은 트랜잭션에서 revoke와 hash/snapshot/binding 제거를 수행한다. handler 밖 SQL과 분석 item 교체에도 적용된다. scope를 되돌려도 기존 링크는 살아나지 않는다. gate의 원본·부모 관계 검사는 trigger와 별도로 유지한다. 만료·고아 payload cleanup은 retention loop에 연결하며 gate 차단을 cleanup 실행에 의존시키지 않는다.
- final gate의 짧은 자원 잠금·share 잠금과 트랜잭션 커밋이 응답 인가 순서를 정한다. 그 뒤 시작한 회수가 이미 인가한 전송을 취소한다고 약속하지 않는다. 테스트는 회수 후 새 응답 차단과 인가 후 전송 완료를 모두 검증한다. 재게시 revision이 바뀌면 생성 결과를 버리고409다.
- 보안 header는 순수 ASGI middleware로 owner 발급·public 성공/오류/HEAD/CORS 응답에도 붙인다. 공개 Uvicorn raw access log를 차단하고 별도 로그에 경로 template/status/새 request ID만 기록한다. audit는 share ID·owner/anonymous·action/outcome·선택 format만 기록한다. 외부 proxy/CDN 로그·프론트 CSP/캐시/실기기 전달은 미검증이며 기능 활성화 조건으로 남는다.
- 기본 `public_sharing_enabled=false`; off일 때 DB 조회·실패 bucket 소모 없이 항상404다. 실제 공개 UI 준비와 전달 환경 검증 전 켜지 않는다. PMS/WBS/Artifact는 `resource_not_enabled`, 프론트는 미수정이다. 단일 프로세스 rate/concurrency 제한은 재시작 시 초기화된다.
- 검증 원문과 명령·미커밋 경계: [핵심 구현 작업기록](../work-history/2026-10-15-public-share-core.md). 새 격리 DB에서만 migration 왕복과 합성 HTTP/트랜잭션 테스트를 실행한다. 운영 사용자 회의/원음/전사·운영 DB·실제 공개 링크·모델·배포/재시작은 다루지 않았다.

## 13. 2026-10-15 TXT/MD/JSON/CSV 및 owner export 후속

- `sharing/exports.py`의 네 pure async formatter는 `MeetingDocumentV1`과 검증된 part만 받는다. JSON은 strict DTO 전체만 직렬화하고, TXT/MD는 선택된 section만 렌더링하며, CSV는 계약 고정 컬럼·UTF-8 BOM·CRLF·writer quoting·텍스트 수식 방어를 적용한다. SRT/VTT/DOCX/PDF는 비활성으로 남는다.
- `POST /api/exports`는 owner `User` 인증, source revision 일치, format/part 적용성 확인 뒤 현재 선택 DTO의 deep copy로 생성한다. 공유 발급이나 Artifact를 만들지 않으며, 공개 공유 기능 설정이 off여도 개인 export는 사용할 수 있다. 응답 파일명은 `meeting-export.<format>` 고정이다.
- 공개 download wrapper는 동일 네 formatter에 승인 snapshot의 deep copy와 검증 part만 넘긴다. 기존 형식·part·revision·생성량/동시성·rate 검사와 `AuthorizedResponse`의 `final_gate`는 유지한다. 생성 중 회수/삭제/만료·재게시가 발생하면 기존 gate가 404/409로 바이트를 차단한다.
- preview `format_options`는 현재 활성 형식과 대상 part를 내보내며, 선택되었으나 미분석인 분석 CSV part는 다운로드에서 `analysis_not_generated`로 실패한다. 분석 완료 후 선택 항목이 비어 있는 CSV는 헤더만 반환한다. summary-only projection은 transcript/job 조회 경로를 실행하지 않는다.
- UI/API 연결 계약은 [API 계약의 회의록 외부 공유와 내보내기](../api-contract.md#회의록-외부-공유와-내보내기)를 따른다. preview가 돌려준 `source_revision`을 게시와 `POST /api/exports`에 보내며, 게시 후에는 발급된 fragment capability를 헤더로 전달한다. `format_options`가 UI의 형식·part 비활성화 기준이다. 공개 기능 기본 off는 유지한다.
- 검증 명령·출력, 미커밋 경계 및 미검증 UI/운영 표면: [2026-10-15 export 후속 작업기록](../work-history/2026-10-15-public-share-exports.md).
