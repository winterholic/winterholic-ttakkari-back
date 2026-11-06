# Frame Animation Benchmark B — 접근 가능한 증거 색인

기록일: 2026-11-07 KST
프로젝트: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`
소유 범위: `frame_animation.py`, `templates/frame_animation/`, `scripts/run_frame_animation_benchmark.py`, 해당 unit tests, `outbox/benchmark_b_work/`, Creative Studio evidence 문서. Core `engines.py`/`dispatch.py`는 변경하지 않음.

## 입력과 제작 소스

- 기능 모듈: `src/ttakkari/creative/v2/frame_animation.py`
- 걷기/인사/점프 키포즈: `templates/frame_animation/presets.py`
- 구조화 입력 storyboard: `templates/frame_animation/benchmark_b_storyboard.json`
- 재현 가능한 제작/출력 스크립트: `scripts/run_frame_animation_benchmark.py`
- 통합 담당자가 작성한 공통 계약: `docs/creative-studio/engine-contract-20261107.md`
- 실제 생성된 입력 spec: `corrected_20261107/run_01_initial/animation_spec.json`, `corrected_20261107/run_02_remediated/animation_spec.json`
- 동일 원본 레이어 9종: 각 재렌더 폴더의 `character_assets/` (몸통, 머리, 표정 normal/smile/wink, 팔 2, 다리 2)

Storyboard는 10초, 16:9, 12fps 제작 / 24fps 재생을 설정합니다. 장면은 걷기 4초, 손 흔들기·표정변화 3초, 점프·착지 3초입니다. `hold`로 각 제작 포즈를 24fps 재생 타임라인에서 유지합니다.

## 실행/QA 기록 및 산출물

- 실제 명령, 종료 코드, 출력, 측정값, SHA-256: `corrected_20261107/EXECUTION_LOG.md`
- 1차 비교 MP4와 이미지 시퀀스: `corrected_20261107/run_01_initial/`
- 수정 Run MP4, WAV, manifest, spec, 240 frame PNG, 20 keyframes, 8 continuous samples: `corrected_20261107/run_02_remediated/`
- 기존 첫 실행 자료도 보존: `run_01_initial/`, `run_02_remediated/`. 기존 보고서/manifest의 과장 수치는 수정해 NOT_RATED 처리했으며 새 모션 계산을 적용한 재현 정본은 `corrected_20261107/`입니다.
- 요약 보고서: `BENCHMARK_B_REPORT.md`, `BENCHMARK_B_REPORT.json`
- 결함 파라미터 기록: `run_01_initial/defect_analysis.json`, `corrected_20261107/run_01_initial/defect_analysis.json`

## 공유 영상 (현재 Run outbox)

Outbox: `/Volumes/Backup/ttakkari-data/outbox/01b88080-f64a-4a92-b55f-24df92644f51/frame_animation_benchmark_b/`

| 영상 | 공유 ID | SHA-256 |
|---|---|---|
| 수정 최종 `ttakkari_2d_animation_final.mp4` | `9a79ae24-5c1e-4837-ab02-513b321b7b3c` | `9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f` |
| 비교용 초기 `animation_run01_initial.mp4` | `df81eae0-3595-4d80-9bc3-d2da622fc929` | `c4b292c5a9b7db78d2c743c21706c15449c23d9b82c5f6f2733e75d9fe5bc6dc` |

## 정정 및 검증 범위

- identity는 같은 레이어 자산을 재사용했다는 구조 사실만 기록합니다. 프레임 간 시각적 동일성 비교는 미실행입니다.
- `motion_continuity_score`는 `null`, rating은 `not_evaluated`, verdict는 `NOT_RATED`입니다. 평균/최대 변위와 120px teleport threshold flag만 출력됩니다.
- 기존 “identity 1.0”, “continuity 0.98”, “조인트 결합 100%/간극 0px”는 직접 비교/측정되지 않아 정정했습니다. 기존 Run 1 결함 `~8px`도 제작 기록의 추정치이며 픽셀 차이 분석은 보존되지 않았습니다. -36px → -24px 피벗 변경은 사실이나 효과 수치는 미검증입니다.
- 재현된 프리셋 테스트는 걷기에서 회전 폭 40° 이상 및 수직 바운스 10px 이상, 인사에서 3개 이상의 팔 각도와 30° 이상 변화 폭을 검사합니다.
- 자동 미디어 QA는 6개 검사 모두 통과했습니다. 청감 품질과 전체 프레임 수동 리뷰는 미실행입니다.
- `message_worker`를 통해 통합 담당자 `86c93b9b-7d99-4814-b83c-0dd77c453b2c`에게 계약을 전달하려 했으나 이 세션의 하위 작업이 아니라며 `404 not_found`가 반환됐습니다. 재시도하지 않았습니다. 실제 입력/출력 계약은 위 소스와 engine-contract 문서에서 확인할 수 있습니다.
