# 2026-10-22 창작 스튜디오 3·4단계: 엔진을 G0에 연결하고 감독·검수·재제작을 붙임

다음 검수자가 이 저장소만으로 맥락을 읽도록 남긴다. 설계 근거와 G0 확장 이유는 `docs/adr/0068-creative-engine-adapters-and-director.md`, 프론트용 API는 `docs/design/creative-production-api.md`다.

- 브랜치: `feat/creative-engines-director` (기준 `origin/main` `2522d78`, G0 `28fa795`·G1 `184f70e` 포함). main 병합·배포 예약은 하지 않았다.
- 작업 위치: 격리 worktree `/tmp/back-g34`. 공유 back 작업트리(로컬 커밋 66c8402, 미커밋 변경)는 건드리지 않았다.
- 범위: 실행 순서 문서의 3단계(엔진 연결)와 4단계(감독·타임라인·검수 연결). 5단계 이후(품질 실험)는 하지 않았다.

## 구현한 것
| 영역 | 파일 |
|---|---|
| 어댑터 계약·레지스트리 | `creative/engines/base.py`, `registry.py`, `media.py`, `node_tools.py` |
| 어댑터 | `ffmpeg_engine.py`, `remotion_engine.py`, `three_engine.py`, `tts_engine.py`, `comfy_engine.py`, 시험용 `mock.py`, 렌더 코드 `engines/assets/` |
| G0 연결 | `creative/pipeline.py`(spec→attempt→receipt, 재개, 취소, 조립), `director.py`(추가 메서드만) |
| 감독 | `direction.py`, `director_llm.py`, `audio_analysis.py` |
| 검수·재제작 | `review.py`, `feedback.py`, `record_view.py` |
| API·문서 | `api/creative.py`(경로 13개 추가), `docs/design/creative-production-api.md`, ADR 0068 |
| 재현 | `scripts/repro/creative_engines_smoke.py`, 증거 `docs/work-history/evidence/2026-10-22-creative-engines-smoke.json` |

## 검증 결과(이 환경에서 실제로 실행)
- 신규 시험 + G0·G1 회귀: `PYTHONPATH=src:unit_tests python -m pytest -q unit_tests/test_creative_*.py unit_tests/test_managed_comfy_receipt.py unit_tests/test_comfy_managed*.py unit_tests/test_host_resource*.py unit_tests/test_gpu_resources.py` → `259 passed, 2 skipped`. 신규 파일별: 계약 28, 실제 FFmpeg 검수·재제작 15, 감독·피드백 14, API 6, G0 확장 6(합계 69). skip 2개는 Chrome을 쓰는 `test_creative_real_engines.py`로 `TTAKKARI_REAL_ENGINE_TESTS=1`일 때만 돈다. 그 환경에서 실행해 `2 passed in 43.16s`였다.
- DB(PostgreSQL 클러스터)가 필요한 `tests/` 경로: 이 브랜치 코드 작성 중 SysV 공유 메모리에 여유가 있던 시각에 `tests/test_route_inventory.py tests/test_host_gpu.py` → `39 passed`, `tests/test_creative_api.py tests/test_security_gate_inventory.py` → `5 passed`였다. 그 뒤 마지막 수정(작업 폴더 정리·shot_id 검증·ffmpeg 옵션·시험·문서)을 마친 최종 코드에서는 아래 이유로 conftest 경로를 다시 통과시키지 못했다.
- PostgreSQL initdb: `tests/conftest.py` 시작 중 `initdb`가 exit 1로 반복 실패했다(연속 4회 이상). 그 시각 SysV 공유 메모리 세그먼트가 `kern.sysv.shmmni=32` 중 거의 모두 다른 세션의 테스트 클러스터로 점유돼 있었다(`ipcs -m`). 실행 환경 한계로 기록하고 우회(남의 세그먼트 정리, 다른 DB)하지 않았다. DB가 필요 없는 인벤토리 시험만 `--noconftest`로 돌려 최종 코드에서 `10 passed`(route_inventory 7 + security_gate_inventory 3)를 확인했다. `tests/test_host_gpu.py`와 `tests/test_creative_api.py`는 최종 코드에서 DB 없이 돌릴 수 없어 **최종 코드 기준 미실행**이다(이 브랜치는 둘이 쓰는 코드를 바꾸지 않았다).
- `ruff check`/`ruff format --check`: 변경·신규 파일 통과. 저장소 다른 곳의 기존 ruff 오류(`tests/game_smoke`, `scripts/game_e2e`, `security/guard.py` 등)는 건드리지 않았다.

### 실제 렌더 smoke(`creative_engines_smoke.py`, Mac Studio, CPU+Chrome, GPU 모델 없음)
| 엔진 | 벽시계 | 프로세스 트리 RSS 표본 최댓값 | 결과(ffmpeg 디코드·ffprobe) |
|---|---|---|---|
| FFmpeg 7.1(번들) solid-color+tone | 0.18초 | 29MB | 24프레임 디코드 오류 0, 320×180 h264 24fps + aac 48kHz |
| Remotion 4.0.537 title-card | 7.4초(이 세션 관측 5.4~14.7초, 번들링 포함·호스트 부하에 따라 변동) | 2006MB(node+Chrome Headless Shell) | 24프레임, 640×360 h264 24fps |
| Three.js 0.185 orbit-scene(SwiftShader CPU WebGL) | 2.9초(smoke 전체 8.2초, 결정성 대조 포함) | 1259MB | 24프레임, 640×360 h264 24fps, 순서 바꿔 렌더한 3개 프레임의 PNG sha256 동일 |
- 혼합 제작 시나리오(Remotion·Three·FFmpeg 3샷 + 클릭 트랙 오디오, 감독 fake LLM) 전체 34.1초: 렌더 6.8/3.2/0.2초, 결함 샷만 재렌더 6.9초, 최종 조립 후 ffprobe `72 frames, 24/1, 3.000s h264 + aac 48kHz mono, 206,221 bytes`. 호스트 가용 메모리 31.8→31.3GB, 스왑 사용량 28.47GB 불변(이 값은 이전부터 쌓인 상태다. 이번 실행의 증가분이 아님).
- 결함 샷 재제작 증거: Remotion 샷에 `fade_out_frames=12`(끝이 검게 꺼짐)를 일부러 설정 → 기계 검수가 `black_frames` fail → 피드백 API 경로로 그 샷만 `fade_out_frames=0`으로 새 revision·attempt 생성 → 샷 2·3의 output sha256 불변, 샷 1만 변경, 결함 해소, 최종 전수 검사 통과.
- 시험이 잡아낸 실제 결함: (1) 같은 바이트를 낸 서로 다른 샷의 검수가 output hash로 섞임 → attempt 기준 조회와 `create_review(attempt_id)`로 수정, (2) FFmpeg `pad`에 `WxH`를 넘겨 조립 실패 → 수정, (3) 조립 결과가 71프레임·24.34fps(timestamp 불균일)였고 최종 검사가 잡음 → `-fps_mode cfr -r`로 수정, (4) 클립 끝까지 이어진 정지 구간의 `freeze_end` 부재로 선언 범위 검사가 통과해 버림 → 클립 길이로 끝을 채워 수정.

## 하지 않았거나 검증하지 못한 것
- **Comfy 실제 생성**: 하지 않았다. 컨트롤러 대역(`FakeComfyController`)으로 제출·재개(중복 제출 없음)·실패·빈 결과·receipt 불일치·해시 불일치·reconcile·취소를 계약 시험만 했고, 그 receipt는 `real_execution=false`다. 이 통과는 실제 생성 성공이 아니다. 실제 `probe`는 파일만 읽어 `present`(모델 3개 확인)로 나왔고 서버를 띄우지 않았다.
- **로컬 TTS 실제 합성**: 하지 않았다(공급자 대역). 실제 Supertonic 런타임 존재 확인도 `probe`만 설계했고 실행하지 않았다.
- **감독 LLM 실제 경로**(`SessionRunDirectorLlm`): 코드만 있고 이 환경에서 실행하지 않았다(DB·Run 인프라 필요). 시험은 고정 응답 fake만 썼다. `Run.request_key` 재사용 동작은 미검증이다.
- 사람 시청/청취, 독립 검수, VLM 의미 검수: 없다. 모든 승인 필드는 null이다. 기계 검수는 의미·미감을 판정하지 않는다.
- 실제 음악 비트 분석 정확도: 합성 클릭 트랙(120BPM)에서 프레임 정렬(12·24·36 프레임)만 확인했다. 첫 박(t=0)은 플럭스가 0이라 잡지 않는다.
- Metal/GPU Three 경로, Blender, ACE-Step 음악 생성, 색 변환·LUT 적용: 이번 범위 밖이며 구현하지 않았다.
- 서버 시작 시 미완료 attempt 자동 재개: 없다. (`run_unattached`의 동시 실행 직렬화는 아래 G34 후속 수정으로 추가됨.)
- 운영 배포: 하지 않았다. 배포 예약도 하지 않았다.

## 다음 검수자가 볼 곳
1. `director.py` 추가 메서드 5개와 `create_review` 확장(ADR 0068 §3 표)이 G0의 CAS·revision·승인 null 규칙을 약화하지 않는지. 시험: `unit_tests/test_creative_director_extensions.py`와 기존 `test_creative_director.py`.
2. 자동 선택 조건(`registry._eligibility_problems`)이 미측정·오래된 측정·mock·Comfy를 정말 배제하는지. 시험: `test_creative_engine_contract.py` 레지스트리 시험.
3. crash 재개·취소·중복 이벤트·revision 충돌·stale 시험: `test_creative_engine_contract.py`.
4. 정상/결함 대조 검수와 선언 예외: `test_creative_review_real.py`.
5. 재현: `PYTHONPATH=src python scripts/repro/creative_engines_smoke.py`(약 35~60초, Chrome 사용, 새 임시 폴더에서만 쓴다).

## G34 독립 검수(FAILED) 후속 수정
검수 보고서(`creative-g34-independent-review`)와 반례 `repro_adversarial.py`에서 재현된 4건을 고쳤다. 회귀 시험은 `unit_tests/test_creative_g34_review_fixes.py`(신규 9)와 `test_comfy_i2v_input.py`(신규 2, 업로드 TOCTOU는 `2026-10-22-e1-i2v-input-contract.md`). 신규 11건 중 7건이 수정 전 코드에서 실패함을 확인했다(`git stash`로 src만 되돌려 실행. 나머지 4건은 이미 맞던 동작을 고정하는 대조 시험이다).

| # | 결함 | 수정 | 시험 |
|---|---|---|---|
| P1 | 미디어 출력 기록 뒤 receipt 전에 죽으면 다른 바이트 재개가 `collect_failed` | 부분 수집 보존(기존 G0 시험) 규칙은 유지하고, 새 claim 시점에 그 attempt의 이전 `outputs`와 `{attempt_id}-N.bin` manifest 항목을 버리며 `attempt.stale_outputs_discarded` 이벤트를 남긴다. 완료/실패/취소 attempt는 재실행하지 않는다 | 크래시 후 다른 바이트 재개 성공, 같은 바이트 멱등, 완료 attempt 출력 불변 |
| P1 | I2V 업로드 TOCTOU | e1 문서 참조 | e1 문서 참조 |
| P2 | `run_unattached` 동시 실행 | `(구역,키)`별 flock(`locked_unattached_execution`) 안에서 sidecar 재확인, 실행, 기록 순서 | 4스레드 같은 키 어댑터 1회, 다른 키는 서로 막지 않음, 실패 후 재시도 |
| P2 | sidecar `content_hash` 덮어쓰기 | `key`, `content_hash`, `at`은 저장소 예약 필드. 해시는 예약 필드를 뺀 payload의 계산값이고 호출자 `content_hash`/`key`가 맞지 않거나 `at`이 있으면 `ValueError`. 같은 키 다른 내용은 여전히 `ConflictError` | 계산값 동일 재호출 멱등, 불일치 명확한 오류, 예약 필드 |

- 원본 반례 재실행(수정 worktree, 경로만 치환한 사본): attempt 크래시 후 `resume_state=review_pending`, `resume_error_kind=null`; sidecar `content_hash=caller-value`는 `ValueError`; staged 변조는 `job_failed=true`, `hash_matches_declared=true`, `prompt_submitted=false`. unattached는 원 스크립트의 fake가 동시 상대를 5초 기다리는 장치라 직렬화되면 첫 호출이 BrokenBarrier로 끝나고 두 번째가 순차 실행돼 `adapter_calls=2`로 보인다. 동시 실행 여부는 회귀 시험(어댑터 1회)이 판정한다.
- [1차 G34 구현 당시 한계 — v2에서 해결·정책 변경] unattached는 sidecar 전 crash 때 다시 실행했고, 비결정적 결과는 이름 충돌할 수 있었다. v2 계약은 아래에 기록한다. (2) 서버 시작 시 미완료 G0 attempt 자동 재개는 여전히 없다.
- 포맷: 보고된 6개 파일을 `ruff format`으로 정리했다(`managed_comfy.py`는 형식만 바뀐 큰 diff). 로직 변경은 `_upload_input`, `_cleanup_input`, `_read_regular_file`뿐이다.
- 실제 GPU/Comfy 실행, main 병합, 배포 예약은 하지 않았다.

## G34 독립 재검수 v2 후속 수정
- 기준: `87c701f4861292e6a508d11840847de865d5454c`, `/Users/winterholic/.ttakkari/data/outbox/c3622ffb-28bf-4330-a235-dda53145eb37/creative-g34-87c701f-review/{review.md,recheck_v2.py,recheck_v2.log,pytest_target.log,lint_checkout.log}`를 먼저 확인했다. 검수 판정은 FAILED. 재현 로그의 observed fact와 아래 code-based intent를 구분한다.
- **Observed I2V:** 실제 lab `ComfyClient.upload_image()`를 사용하고 urllib만 stub한 재검수에서 전송 multipart image SHA `fd7b…fca1e`, 선언 SHA `3c74…494b2`, `controller_accepted=true`였다. 같은 UID 로컬 write actor가 비공개 snapshot을 client read 동안 바꿨다가 복구했다. 이 관측은 해당 로컬 전제이며 원격 API 경로 노출을 의미하지 않는다.
- **Observed unattached:** 같은 key의 spec/profile/extra가 달라도 이전 sidecar를 반환했다. 또 재검수 스크립트의 crash case는 spec version A→B도 함께 바꿨으므로 그 출력만으로 crash 복구를 판단하지 않았다. 별도 재현에서 spec·extra·input refs를 그대로 두고 non-deterministic adapter output A→B로 분리해 테스트한다.
- **I2V 수정:** `_upload_input`은 `O_NOFOLLOW` fd에서 읽어 해시를 확인한 immutable bytes를 저장소 소유 HTTP multipart integration으로 직접 직렬화한다. path-only 외부 client를 변경하지 않았다. 회귀는 실제 lab `ComfyClient` instance와 urllib transport stub을 사용해 multipart image 파트의 SHA를 읽는다. API 경로는 비공개이며 `uploaded_sha256`을 upload record에 남긴다. 서버 저장 파일 hash는 응답에 없어 미검증이고 실제 Comfy/GPU는 실행하지 않았다. 세부는 `2026-10-22-e1-i2v-input-contract.md`의 v2 항목과 ADR 0068.
- **요청 identity:** `run_unattached`는 canonical `section/key/spec/extra` hash를 sidecar에 저장한다. 동일 identity만 재사용하고 spec·extra·input ref가 다르면 adapter 전에 `ConflictError`; identity 없는 legacy sidecar는 자동 hit로 쓰지 않고 명시적 조정/새 key를 요구한다. 같은 key의 경쟁 요청은 flock으로 하나만 실행되고 승자 identity와 다른 요청은 충돌한다.
- **Crash 계약:** `unattached_executions`에 외부 호출 전 `executing`을 기록하고 모든 결과 CAS 뒤 `completed` checkpoint를 기록한 다음 sidecar를 쓴다. checkpoint 뒤 sidecar crash는 동일 결과를 복구하고 adapter를 다시 부르지 않는다. `executing`만 남은 경우는 실행 완료 여부가 불명이라 기본 재호출을 차단한다. caller가 외부 상태를 조정한 뒤 `retry_unknown_execution=True`로 명시 재시도할 수 있고 duplicate external work 가능성은 남는다(exactly-once 미보장). artifact 이름은 request identity+output SHA를 사용해 재시도 비결정 출력의 동명 충돌을 피한다. 미완료 G0 attempt/terminal sibling/input 보존 규칙은 수정하지 않았다.
- **회귀 추가:** `test_comfy_i2v_input.py`에 실제 client multipart bytes hash 관측; `test_creative_g34_review_fixes.py`에 same-key spec/extra/input ref 충돌, 다른 요청 동시 경합, 2-process 동일 요청 단일 실행, output CAS 전 및 CAS 후/checkpoint 전 unknown 상태, checkpoint 완료 후 sidecar crash 복구, terminal G0 attempt·sibling sidecar·input 보존, legacy sidecar 거부를 추가했다.
- **이번 작업 검증:**
  - `PYTHONPATH=src:unit_tests:/Users/winterholic/development/projects/ttakkari-studio-lab/comfyui/adapter .venv/bin/python -m pytest -q unit_tests/test_creative_g34_review_fixes.py unit_tests/test_comfy_i2v_input.py unit_tests/test_creative_engine_contract.py unit_tests/test_creative_director_extensions.py unit_tests/test_creative_comfy_i2v.py unit_tests/test_comfy_managed_protocol.py unit_tests/test_comfy_managed.py unit_tests/test_managed_comfy_receipt.py` → `138 passed in 12.07s`.
  - `.venv/bin/ruff check src/ttakkari/api/creative.py src/ttakkari/creative/director.py src/ttakkari/creative/pipeline.py src/ttakkari/managed_comfy.py unit_tests/test_comfy_i2v_input.py unit_tests/test_creative_g34_review_fixes.py` → `All checks passed!`.
  - `.venv/bin/ruff format --check src/ttakkari/api/creative.py src/ttakkari/creative/director.py src/ttakkari/creative/pipeline.py src/ttakkari/managed_comfy.py unit_tests/test_comfy_i2v_input.py unit_tests/test_creative_g34_review_fixes.py` → `6 files already formatted`.
  - `git diff --check` → 종료 코드 0, 출력 없음.
  - 커밋 `a7001af` (`fix(creative): bind I2V bytes and checkpoint unattached runs`)을 `origin/feat/creative-engines-director`에 push했다. 결과: `87c701f..a7001af feat/creative-engines-director -> feat/creative-engines-director`.
  - 이 검증 범위는 요청된 회귀 묶음이다. 전체 저장소 pytest, DB/host-resource 검증은 실행하지 않았다. 실제 Comfy HTTP server·GPU·TTS·감독 LLM·Chrome도 실행하지 않았다. main 병합과 배포 예약은 하지 않았다.
