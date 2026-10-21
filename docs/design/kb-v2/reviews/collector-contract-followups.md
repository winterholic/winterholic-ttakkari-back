# collector 소유자에게 반환할 계약·코드 변경 요청

2026-10-21 / 검토대상8e3dc2c / F1·F10·F11. 이번 작업은 kb 설계 문서만 수정했으며 collector 제품 코드는 변경하지 않았다. 수집 플랫폼은 지식 모델을 import하거나 kb 표를 조회하지 않는다.

| 요청 | 계약/필요 코드 | 완료 검증·차단 단위 |
|---|---|---|
| 최종 NFC | zero-width/control 제거·공백 정리 **뒤** NFC, 최종 text UTF-8 sha, normalizer_version 증가. 구 payload 수정 대신 새 envelope ID로 lineage 연결하여 재발행 | e+soft hyphen+accent, 한글jamo+ZWJ, a+ZWNJ+accent, e+control+accent; NFC고정점·sha·멱등. KB5 strict sink 활성화 전 |
| 모든 관측 identity | 고유 수집 event마다 stable observation/envelope ID, event time·외부 version·receipt를 구분. 전달 재시도 동일 ID, 내용 동일 새 수집은 새 ID | A→B→A/A→A·늦은옛snapshot·동률/시계오류. KB4/5 |
| pin API 소유 | collector pin registry와 pin/unpin/revoke/status 포트. 같은 DB 수용 transaction을 전달받는 API; collector는 kb_sources 참조검사하지 않음 | GC/pin 경합, transaction rollback 후 고아pin, 동일 idempotency key. KB5 |
| 권리 우선 revoke | retain_until/삭제/철회가 pin보다 우선. durable revoke outbox+재시작 overdue 조정·ack; BlobStore read fence 및 kb tombstone 통지 | 삭제 중실패/재개·replica·백업복원·같은 blob alias 재노출0. KB4/5/6 연동 |

권리/producer 버전 계약 합의와 실제 DB 독립 시험 전 완료 처리하지 않는다. 공유 dirty 코드의 현재 변경 내용이 위 요청을 이미 만족하는지 이번 문서 수정으로 단정하지 않는다.
