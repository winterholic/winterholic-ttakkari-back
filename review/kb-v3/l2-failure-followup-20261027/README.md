# L2 검증 operation 반례와 교정 착수 — 2026-10-27

현재 root 후보 `a873a2a650e4ae3e627346056a82a67826e41e97`의 `src/ttakkari/operations/kb_v3_verify.py`가 독립 검토 대상 `f774928b2c999da28e33e5139841742ed8544f56`와 바이트 동일한 코드임을 git diff exit 0으로 대조했다. 리드는 검증자가 보존한 probe를 현재 root PYTHONPATH에 직접 실행했다. 실제 PDF 파서 및 격리 in-memory SQLite만 사용했으며 PG·운영·모델 CLI는 호출하지 않았다. 개발 설정 경고는 이 probe 환경의 출력이며 운영 설정 판정이 아니다.

직접 실행 결과:
- PDF 1359 bytes, 실제 pdf_pypdf_v1, 3 pages, 247 chars, locator spans 3, succeeded.
- held jobs 3개·L2 calls 0·assertions/evidence/conflicts 0에도 overall=succeeded. probe exit 0은 결함 재현에 대한 단언이 맞았음을 뜻하며 제품 검증 통과가 아니다.

원시 명령·exit·scope·script SHA는 commands.json, 출력은 root-probe.log.gz다. 원출력의 공백을 보존하도록 무손실 gzip과 SHA-256으로 저장했다. probe.py는 해당 실행 시 원본과 같은 SHA의 사본이다. script의 target 문자열은 기준 f774를 나타내며 이번 실행 checkout은 commands.json의 a873이다.

검증자 ff7e의 실행 receipt는 codex/gpt-6-luna, task_type=verification을 확인했다. 원본 공유 트리가 검증 중 1e5d7bb2로 이동했고 원래 구현자가 검증자의 진행 중 파일까지 그 커밋에 포함했다. 리드는 그 커밋을 병합하지 않았으며 검증 원문 작성 소유자는 ff7e로 유지한다. 1e5의 held 부분교정과 제품 내부 검증자 추천 메타데이터도 채택하지 않았다. 거짓 성공 외 글로벌 queue pump·공유 tasks gather·시간상한·inline 취소 지적은 현재 코드 경로 기반이며 이번 리드 probe가 그 동작까지 실증한 결과는 아니다.

조치: 기존 구현자 26fff의 자동 후속/대기를 stop_worker로 취소하여 진행 중 파일 영향을 멈췄다. 이미 커밋된 제품을 보존한 뒤 같은 작업자에게 fresh_context, task_type=general로 새 교정 Run `14b1c125-6847-4351-822e-2e315ae84a7b`를 요청했다. 새 tree `back-kb-v3-operation-fixes-20261027`를 a873에서 생성하고 원본 검증 tree는 수정하지 않도록 범위를 고정했다. 리드는 worker metadata에 verification_outcome=failed를 정확 f774 범위로 기록했다.

교정 범위는 실제 L2·현재 원문 인용·정책 두 문서 간 상충 성공 조건, 정확 3잡만 관측하는 유한 대기, 기존 background operation 소유·단일 슬롯·취소·감사 계약 재사용, 멱등 재실행 및 stale 결과 배제다. 개발 작업자의 Flash 라우팅 정책은 제품 JSON에 넣지 않는다. 필요한 음성 RED→GREEN 및 관련 회귀 원출력을 요구했다.

앞선 root 관련 58회귀는 바뀌지 않은 기본 설정/예산/스키마 범위에서 유효하나 operation 승인을 대신하지 않는다. 새 교정의 영향에 따라 해당 범위만 추가 검증한다. main/push/deploy 및 운영 operation 호출은 아직 하지 않았다. 교정 결과 수신→타 공급자 독립 검증→root 통합/관련 회귀→배포 예약의 순서로 이어간다.
