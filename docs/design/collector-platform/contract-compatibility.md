# 기존 KB 수집 계약의 확장과 경계 호환표

마감 재대조: 로컬 HEAD `c12eff6e753e9f438f37d1fc74999d5e47ced31a`, KB 커밋 `63cf7e02f052d3c4ebbfd9cc820257a8c40ea649`. 읽은 문서: `docs/adr/0063-knowledge-library.md`, `docs/kb/design-v1.md:497-521`, `docs/design/knowledge-library.md`의 D1/D10, 현재 `kb_store.py:80-170`, `kb_models.py`, `0055_kb_immutability_hardening.py`. 이번 수정은 수집 플랫폼의 새 문서에만 반영하며 다른 세션의 KB 문서를 덮어쓰지 않는다.

> 2026-10-21 경계 정정: 위 SHA/시점의 재대조 이력은 보존한다. 후속 활성 KB 계약은 [v2 §7/§12](../kb-v2/knowledge-library-design-v2.md)이며 [생산자 변경 요청](../kb-v2/reviews/collector-contract-followups.md)은 미구현/협의 대상이다. 아래 v1 Collector Protocol은 역사적 개념 매핑이며 collector가 KB에 종속된다는 뜻이 아니다.

## 기존 계약을 확장한다

KB 설계의 `Collector(Protocol)`은 `name/version`, `discover(spec,cursor)→DiscoverPage`, `fetch(item)→RawArtifact`, `parse(raw)→NormalizedDoc`를 이미 제안한다. 이것은 함수형 adapter 계약이며 범용 버전 JSON 교환 schema가 아니다. 본 설계는 이 흐름을 그대로 수용하고 ingestion envelope를 출력/소비 경계에 추가한다. KB 문서의 블롭·재시도·robots·수집 job 공통부를 또 하나의 scheduler로 구현하지 않는다. collector 공통부가 이 책임을 맡고 KB adapter는 passage/index/claim/canonical 저장만 맡는 방향으로 다음 구현 소유자와 정합화한다.

| 기존 Protocol/KB 개념 | IngestionEnvelope 확장 | 호환 조건 |
|---|---|---|
| Collector.name/version | producer.id/version | immutable parser/scorer version은 별도 필드 |
| DiscoverPage/cursor | durable CollectionJob/StageItem·checkpoint | cursor는 envelope가 아닌 실행 원장에, page accounting과 원자 commit |
| RawArtifact bytes/mime/URL/fetched_at | raw.blob_handle/sha256/byte_size/media_type + source.url/collected_at | raw bytes를 mutable normalized 텍스트로 대체하지 않음 |
| RawArtifact HTTP meta | extensions의 `collector:http_meta` | status/ETag/Last-Modified 등 비민감 allowlist만, 쿠키/인증 헤더 X |
| NormalizedDoc text/language | normalized.text/sha256/language/parser/normalizer/chunker | 생산자 최종 NFC/LF·최종 text SHA, 저장 본문 Unicode codepoint offset. NFKC는 kb 색인 비교용, raw offset과 혼동 X |
| 절 구조·제목/저자/버전 힌트 | extensions의 `kb:normalized_doc` | title/authors/sections/target_version_hints를 손실 없이 보존하고 수신측 strict adapter validator가 검증 |
| 문서 정규키 | source.key + dedupe/canonical_candidate | source identity·content revision·canonical 결정 분리 |
| 출처 T1~T5/visibility | extensions의 `kb:source_policy` + rights/access | trust tier는 품질 확률이 아님. visibility는 private scope를 자동 public 승격하지 않음 |
| 원장 status/retry/robots | collector durable job + errors/decision/rights | collection 실행 원장 한 곳만, KB 자체 import receipt는 소비 이력 |
| KB passage/evidence | adapter가 normalized SHA/구간을 검증해 passage/evidence 생성 | 외부 원전 span만 정본 근거. synthetic/teacher label은 KB evidence로 승격 X |

예시 확장(형식 설명용, 수집/권리 허용 증거 아님):

```json
{
  "kb:normalized_doc": {
    "title": "합성 계약 예시",
    "authors": [],
    "sections": [],
    "target_version_hints": []
  },
  "kb:source_policy": {"trust_tier": "T5", "visibility": "private-note"},
  "collector:http_meta": {"status": 200, "etag": null, "last_modified": null}
}
```

현재 core JSON schema는 namespaced extensions의 키 형식을 검증한다. 위 KB extension payload의 자세한 구조·offset/hash·visibility 교차 검증은 다음 adapter 구현의 명시적 검증 책임이며 이번 core fixture만으로 검증 완료라고 하지 않는다. 이 차이를 숨기지 않고 S2-08 완료기준에 포함한다. 알려지지 않은 KB extension version은 sink가 quarantine해야 한다.

## 새로 확인한 코드와 잔여 차이

- ADR 참조 충돌은 `0063`으로 수정됐고 0055에서 UPDATE/DELETE 불변성과 RESTRICT FK 보강이 들어왔다. 초기 미추적/참조 충돌 관찰은 해결 전 시점의 이력으로만 보존한다.
- content-addressed 저장소는 현재 `kb_store.originals_root()/object_relpath()`를 재사용 후보로 우선 삼는다. 다른 저장소를 복제하기 전에 neutral BlobStore port로 추출/공유하는 최소 변경을 소유자와 합친다. 원본 파일을 고아 정리 도구가 다른 소비자의 살아 있는 참조까지 제거하지 않도록 ref ownership을 확인한다.
- 현재 `ingest_source`는 `(source_key,sha256)` 기존 행을 찾으면 parser_version과 무관하게 기존 revision을 돌려준다. raw bytes를 `normalize_text`로 decode해 body로 만든다. HTML 추출 본문/동일 raw의 새 parser 재처리를 넘기는 직접 함수가 완성됐다고 볼 수 없다. 이는 정적 코드의 계약 차이이며 운영 실패 원인으로 단정하지 않는다.
- KB 설계는 공용 지식의 visibility(public/licensed/private-note)를 제안하고 collector는 기존 tenant/scope를 수집 ACL에 보존한다. 현재 함수에 scope 인자가 없다는 사실만으로 public 운영 누출을 주장하지 않는다. adapter는 권리·principal/visibility 교차를 명시적으로 검증하고 허용 목적에 맞는 데이터만 넘겨야 한다.
- 과거 개인 `knowledge_*` 원장의 5.4.7.3.6 gate는 그 원장으로 첫 의미 추출할 때 실제 선행이다. 범용 `kb_*`의 단순 원본/패시지 import를 이 개인 원장 gate 때문에 차단하지 않는다. S3-10에 연결한 P0 선행은 legacy knowledge 의미 추출 분기를 포함한 보수적 통합 gate이며, 다음 구현이 kb-only로 좁히면 기존 원장 owner와 선행 범위를 정정한다. collector/export/teacher-off 경로는 독립이다.

운영 revision·runtime path·DB schema 적용·MCP 응답·resume는 이번 단계에서 미검증이다. 새 커밋의 source presence와 운영 활성화를 분리한다.
