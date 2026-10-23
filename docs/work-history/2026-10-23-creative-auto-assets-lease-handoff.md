# 2026-10-23 크리에이티브 v2 자동 소재 생성의 `LeaseBusy` 근본 원인과 수정

## 증상
계획 5809bb7f-4800-44d9-aad6-fcdd9a1d03b8 rev1(R21, 3장면)의 `creative_v2_auto_assets_start`가 두 번 모두 장면 Comfy 성공 직후 `LeaseBusy: host GPU resource is owned by another runtime`로 실패했다. s3·Supertonic·ACE-Step은 시작도 못 했다.

## 근거: 외부 보유자가 아니라 자기 앞 장면
`creative_v2_production_status`가 돌려준 생성 기록의 progress 시각(UTC)이다.
| 시각 | 사건 |
|---|---|
| 05:38:33.830 | s1 `scene_comfy_succeeded` (1차 시도) |
| 05:38:46 | 2차 시도 시작(재시도), s1 재사용 |
| 05:38:47.085 | s2 `scene_comfy_submitted`: s1 lease가 이미 풀려 prepare 성공 |
| 05:39:24.119 | s2 `scene_comfy_succeeded` |
| 05:39:24.188 | 실패 기록. 성공 기록 70ms 뒤 |

두 번 모두 "성공 기록 직후 다음 prepare"에서 실패했고, 13초 뒤 재시도에서는 같은 prepare가 통과했다. 외부 프로세스가 점유했다면 이 결정적 패턴이 나오지 않는다.

## 코드 경로
- `managed_comfy.ComfyManagedController.execute()`는 job을 `succeeded`로 저장(`_save`)한 뒤 `_stop_release()`에서 Comfy를 멈추고 `lease.release()`를 호출한다. Comfy 정지는 수 초 이상 걸린다.
- `ManagedAssetGenerator._run_comfy()`는 `controller.get()`이 `succeeded`를 반환하면 바로 다음 장면 `prepare()`를 호출한다. `prepare()`는 `ResourceLeaseManager.acquire()`로 `host-gpu/lock` flock을 비차단으로 잡으므로 앞 장면의 flock이 아직 살아 있으면 `LeaseBusy`다.
- 같은 구조가 마지막 장면과 `_generate_music()`의 ACE-Step lease 사이에도 있었다. Supertonic은 lease를 쓰지 않는다.
- flock 충돌이라 `ops_search_logs`에는 남지 않았다(`LeaseBusy`·`another runtime` 검색 0건). 소유 기록 파일(`data_dir/host-resource/lease.json`)은 원격 실행 보호 영역이라 읽지 못했다. 소유자 확정은 위 시각 상관관계와 재현 테스트에 근거한다. 실패 시각의 lease.json 실물은 미확인.

## 수정
1. `host_resource.ResourceLeaseManager.acquire(wait_seconds, poll_seconds)`: 일시적 점유(flock 충돌)에 한해 제한 대기. 만료·읽기 불가·legacy 기록은 사람 reconcile이 필요하므로 대기하지 않고 즉시 실패한다. 실패 문구에 점유자(owner_task, family, runtime, phase)와 대기 시간을 넣는다. 기본값 0이라 기존 호출은 동작이 같다.
2. `ComfyManagedController.prepare(lease_wait_seconds)`로 전달. 생성기는 장면 Comfy와 ACE-Step lease에 180초 인계 대기를 쓴다.
3. comfy_job 404: 자동 생성 작업의 owner는 `creative-v2:<plan>:<rev>:<scene>`이고 MCP는 `agent:<세션>` owner로만 조회해 설계상 404였다(comfy_jobs 목록도 비어 있음). `GET /api/agent/comfy/jobs/{id}`만 `creative-v2:` 접두사 owner를 읽기 전용으로 허용한다. 취소·reconcile은 계속 소유자 일치를 요구한다. 목록 API는 바꾸지 않았다.

## 테스트
`unit_tests/test_creative_v2_lease_handoff.py` 7건. 수정 전 코드에서는 7건 모두 실패, 수정 후 통과. 느린 stop을 가진 가짜 런타임으로 "succeeded 기록 후 lease 해제 전"을 재현한다.

## 미확인·남은 것
- 운영 재시도 결과(배포 후 계획 5809bb7f 재시도)는 미실행.
- 미확정 Comfy 작업 2ac86aa4…는 건드리지 않았다.
