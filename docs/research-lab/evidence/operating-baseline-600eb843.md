# 배포 전 운영 연구 데이터의 읽기 기준

2026-10-26, 배포 `b8bb518b`는 이번 Run의 한 번 조회에서 `testing`,
`skip_tests=false`, `error=null`이었다. 배포를 재조회하거나 새로 예약하지 않았다.

실제 명령 `curl --fail --silent --show-error http://127.0.0.1:8787/api/version`:

```json
{"revision":"600eb8432f7e1a3e64a9f3a07f175cd183fb90eb","pid":6372}
```

따라서 다음 MCP 읽기는 새 v2 배포 후의 검증이 아니라 기존 운영 버전의 기준이다.

- `manage_research(operation="list")`: 이 개인 세션 범위에서 연구 1개.
- `manage_research(operation="resume", study_id="eb41e3a7-43cd-4f2b-8c03-79380a2e1163")`:
  nodes=12, relations=9, experiments=0, timeline=45, evidence=10, truncated=false.
- 변경·실험 실행·중단·노트 추가는 하지 않았다.

원래 응답은 이번 Run outbox의 `operating-research-before.json`에 보관했다.
자료의 본문을 저장소에 복제하지 않고 [필드별 fingerprint](operating-baseline-600eb843.json)를 커밋한다.
fingerprint는 각 행 ID, 전체 행 SHA256, 기존 각 필드의 SHA256으로 구성한다.
해시는 `json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))`의 UTF-8에 SHA256을 적용했다.
fingerprint 파일은 66,354 bytes, SHA256 `2c4988f35a71a5edae8f0961438afbd6b365c6dae68076c9a3120fbf808db55e`다.

활성화 후 같은 연구를 운영 MCP resume으로 읽고 기준의 모든 행 ID·필드 hash를 대조한다.
새 필드·행은 추가로 구분하고, 기존 값 변경·누락은 따로 보고한다.
이 기준 하나를 운영 전체 DB의 row 수·마이그레이션 lock 시간·타 scope 보존 증거로 확대하지 않는다.
기존 PMS 6.3의 `92b3c8e7`은 이 목록에 없으며 full UUID도 아직 확인하지 못했다.
recall 검색 및 work-history 2026-10-20/21/22/25의 정확한 prefix 검색에는 일치가 없었다.
목록의 범위나 미식별 상태만으로 해당 연구가 삭제됐다고 단정하지 않는다. 이 항목은 미검증이다.

제품 코드 변경이 없으므로 현재 controller의 eb3bcf6 검사를 유지한다.
새 runtime 활성화·새 MCP 생성/실행/실패/재개 인수·실제 Metal·frontend 배포는 계속 미완료다.
