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
- 서버 시작 시 미완료 attempt 자동 재개와 `run_unattached`(TTS·최종 조립)의 execution claim: 없다.
- 운영 배포: 하지 않았다. 배포 예약도 하지 않았다.

## 다음 검수자가 볼 곳
1. `director.py` 추가 메서드 5개와 `create_review` 확장(ADR 0068 §3 표)이 G0의 CAS·revision·승인 null 규칙을 약화하지 않는지. 시험: `unit_tests/test_creative_director_extensions.py`와 기존 `test_creative_director.py`.
2. 자동 선택 조건(`registry._eligibility_problems`)이 미측정·오래된 측정·mock·Comfy를 정말 배제하는지. 시험: `test_creative_engine_contract.py` 레지스트리 시험.
3. crash 재개·취소·중복 이벤트·revision 충돌·stale 시험: `test_creative_engine_contract.py`.
4. 정상/결함 대조 검수와 선언 예외: `test_creative_review_real.py`.
5. 재현: `PYTHONPATH=src python scripts/repro/creative_engines_smoke.py`(약 35~60초, Chrome 사용, 새 임시 폴더에서만 쓴다).
