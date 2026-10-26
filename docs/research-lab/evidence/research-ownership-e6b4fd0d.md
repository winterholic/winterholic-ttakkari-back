# Research 실행 소유권과 중단 복구

2026-10-26, 리드 Codex 직접 구현·검증. 시작 제품은
`e6b4fd0df9513058f9e30845c08f4b0c9172b095`이며 PMS7.3 일부다.
전체 목표와 기존 완료 조건은 active로 유지한다. 새 worker는 배정하지 않았다.

## 범위와 기존 증거

누적 예약 `8e67d4df-c113-4925-924f-70b49192b4b1`은 requested/error=null이었다.
복원 도구 출력을 과다 출력해 구조화 값을 저장하는 과정에서 같은 상태를 한 번 더
읽었다. 이후 추가 조회·대기·벤치는 하지 않았다. requested는 운영 활성화가 아니다.
기존 A~D/UI/575필드·105/9 검증을 원래 범위로 재사용한다. 새 소유권 경계가
바뀐 러너와 세대 코드에 한해 검증했다. 에이전트 RuntimeLease는 에이전트 Run을
참조하므로 연구 실행 소유권과 혼동하지 않는다.

## 변경

- Run 생성 transaction에 `environment_snapshot.research_execution_owner`를
  함께 저장한다. 계약 버전·소유 세대·토큰·수락 시각을 기록한다. 새 테이블이나
  기존 데이터 backfill은 없다. 어댑터 환경값과 합치되 소유권을 덮어쓰게 하지 않는다.
- 기존 generation의 nonblocking flock 구현을 재사용한다. 배치의 queued 형제부터
  어댑터 실행·자원 정리가 끝날 때까지 같은 잠금을 유지한다. 파일 inode를
  교체하지 않도록 잠금 파일을 unlink하지 않는다. heartbeat 만료로 빼앗지 않는다.
- 수락부터 태스크 첫 실행 사이의 예약도 `can_retire()`에서 확인한다. 첫 명령
  전 취소는 done callback이 잠금을 반환한다. 기존 accepted-work gate에는 수락
  세대를 전달하여 다른 세대의 예약을 실행하지 못하게 한다.
- 성공·아티팩트 최종 commit·취소·예외 저장 전에 소유 토큰을 대조한다.
  바뀐 토큰의 오래된 결과는 버리고 이미 존재하는 checkpoint를 덮어쓰지 않는다.
- local/remote API의 active lifecycle과 legacy 주기 작업에서 복구한다. 해당 OS
  잠금이 해제된 queued/running만 `owner_lost`로 cancelled/failed 처리한다.
  기존 성공·원본·환경·측정·아티팩트는 보존하고 불변 `execution_finished`
  타임라인에 중단 사유를 기록한다. 무소유 legacy·알 수 없는 계약은 변경하지 않는다.
  복구는 어댑터를 호출하지 않으며 외부 GPU/TTS 자식 정리는 미확인으로 남긴다.

## 반례와 실행 증거

격리 실제 PostgreSQL의 DB/user를 주입된 시험 URL과 대조하고 운영 DB `ttakkari`를
거부하는 기존 seed를 재사용했다. 어댑터는 합성 ProbeAdapter다. 실제 backend나
host GPU를 중단하지 않았다. subprocess는 시험용 파일 잠금을 잡은 Python이며
SIGKILL 뒤 OS 잠금 해제를 관찰했다.

```text
초기 소유권/복구: 3 failed, 34 deselected in 0.63s
  owner=None / recover_orphaned_runs 부재
수락 예약/drain·lifecycle 연결: 2 failed, 37 deselected in 0.65s
  can_retire=True / recovery 주기 작업 부재
오래된 토큰 결과: succeeded != running (신규 반례)
새 API/research resume: 중단 event 집합이 비어 있음 (신규 반례)
다른 세대 예약: adapter.calls=1 != 0 (신규 반례)
```

첫 task-before-start fixture는 모든 asyncio.create_task를 취소해 SQLAlchemy
session.close까지 취소했다. fixture를 연구 태스크에만 제한했다. 이는 제품 RED로
세지 않는다. 환경 snapshot 동등성의 기존 3개 검사는 새 소유권 필드까지 명시해
기존 환경 값과 토큰을 함께 검증한다. API의 batch spy도 새 private 인자를 받도록
갱신했다. 해당 파일의 기존 import 정렬 1건은 필수 lint 조건에 맞췄다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -k 'durable or owner_process_exit or recovery_does_not_guess or reserved_research or recovery_is_wired or before_background or stale_research or generation_cutover or batch_rejects_reservation' -q --timeout=15 --tb=short
9 passed, 34 deselected in 1.04s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_generation_deployment.py -k 'retire or cutover or overlapping or controller_cuts' -q --timeout=30 --tb=short
6 passed, 43 deselected in 0.68s
$ .venv/bin/ruff check <변경 Python 6파일>
All checks passed!
$ git diff --check
exit0
```

9건은 실제 파일 잠금의 생존·SIGKILL, 측정 checkpoint 보존, 별도 HTTP Run GET과
study resume의 원본·소유권·중단 타임라인, 무소유 legacy/비정상 token 거부,
첫 명령 전 취소, stale fencing, 실제 GenerationRuntime DB cutover/drain,
다른 세대 수락 거부, lifecycle 연결을 포함한다. 죽은 실행은 재호출해도
어댑터 호출 0이며 복구 재호출도 변경 0이었다.

이전 중간 통합 실행은 러너/API/CPU 조합/GenerationRuntime 135PASS8.93초였다.
그 뒤 중단 event와 수락 세대 대조를 추가했으므로 최종 제품 인수는 마지막
9건과 필수 push hook의 변경파일 결과를 기준으로 한다. 묶음끼리 합산하지 않는다.
최종 source hash와 명령은 동명 JSON에 남겼다. pre-push의 계약/변경파일 검증과
원격 SHA·배포 예약 결과는 이후 실행 영수증으로 별도 기록한다.

## 미완료

학습/추론 native checkpoint 재개, 외부 자식의 crash 후 소유 정리·재개,
실제 메모리 상한, 운영 동일 A launch의 Metal 배치, 인증 UI/원문 UX10,
legacy92·PMS7.x 잔여와 전체 배포 gate는 이 결과가 증명하지 않는다.
독립 worker 검증·운영 프로세스 crash 시험도 하지 않았다. 기존 수락 조건을
좁히거나 모든 요구를 done으로 바꾸지 않는다.
