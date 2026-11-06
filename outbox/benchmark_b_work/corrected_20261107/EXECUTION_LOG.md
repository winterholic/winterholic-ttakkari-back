# Benchmark B 재렌더 실행 기록

재렌더 전 HEAD: `5f5cbd997addcb4ea084ab908790f8f6eec54122` (로컬 변경은 요청대로 경로 지정 커밋)

## 렌더 명령

```sh
UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=. uv run python scripts/run_frame_animation_benchmark.py --outbox-dir /Volumes/Backup/ttakkari-data/outbox/01b88080-f64a-4a92-b55f-24df92644f51/frame_animation_benchmark_b --run-id corrected_20261107
```

Exit code: `0`. 관측 출력:

```text
=== [Benchmark B] 2D Frame Animation Production & Defect Remediation ===
[1/6] 캐릭터 레이어 원본 보존 완료: 9개 레이어 (/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107/outbox/benchmark_b_work/corrected_20261107/character_assets)
[2/6] 엔진 어댑터 사전 점검 (Preflight): ok=True, version=ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers
[3/6] Run 1 (초기 렌더링 - 결함 진단용) 시작...
  -> Run 1 렌더 완료: /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107/outbox/benchmark_b_work/corrected_20261107/run_01_initial/animation_run01_initial.mp4
[4/6] Run 2 (결함 수정 및 품질 개선 최종 렌더) 시작...
  -> Run 2 최종 렌더 완료: /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107/outbox/benchmark_b_work/corrected_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4
[5/6] 산출물 아웃박스 복사...
[6/6] 모든 산출물 생성 및 복사 완료! 위치: /Volumes/Backup/ttakkari-data/outbox/01b88080-f64a-4a92-b55f-24df92644f51/frame_animation_benchmark_b
```

## 테스트 명령

```sh
UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=. uv run pytest unit_tests/test_frame_animation_benchmark_safety.py unit_tests/test_creative_v2_frame_animation.py -q
```

Exit code: `0`; 출력: `......... [100%]` / `9 passed in 2.73s`.

## Preflight·probe·QA·motion_eval 명령

```sh
UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=. uv run python -c "import json; from pathlib import Path; from ttakkari.creative.v2 import frame_animation as fa, engines; p=Path('/Volumes/Backup/ttakkari-data/outbox/01b88080-f64a-4a92-b55f-24df92644f51/frame_animation_benchmark_b/ttakkari_2d_animation_final.mp4'); m=json.load(open('outbox/benchmark_b_work/corrected_20261107/run_02_remediated/render_manifest.json')); print('preflight=',fa.preflight()); print('probe=',engines.probe(p)); print('qa=',engines.qa_media(p,aspect='16:9',planned_seconds=10.0,tolerance=0.4)); print('motion_eval=',m['motion_eval']); print('keyframes=',len(m['sampled_keyframes']),'continuous=',m['continuous_samples_count'])"
```

Exit code: `0`; output:

```text
preflight= Preflight(ok=True, version='ffmpeg version 7.1 Copyright (c) 2000-2024 the FFmpeg developers', missing=[])
probe= {'duration': 10.0, 'width': 1280, 'height': 720, 'fps': 24.0, 'has_video': True, 'has_audio': True}
qa= {'passed': True, 'checks': [{'id': 'video_track', 'passed': True, 'detail': '영상 트랙', 'measured': True}, {'id': 'audio_track', 'passed': True, 'detail': '오디오 트랙', 'measured': True}, {'id': 'resolution', 'passed': True, 'detail': '기대 1280x720', 'measured': '1280x720'}, {'id': 'duration', 'passed': True, 'detail': '계획 10초 ±0.4초', 'measured': 10.0}, {'id': 'not_black', 'passed': True, 'detail': '검은 화면 비율 90% 미만', 'measured': 0.0}, {'id': 'audible', 'passed': True, 'detail': '오디오 최대 음량 -60 dB 초과', 'measured': {'mean_db': -14.5, 'max_db': -1.7}}], 'probe': {'duration': 10.0, 'width': 1280, 'height': 720, 'fps': 24.0, 'has_video': True, 'has_audio': True}}
motion_eval= {'same_source_layer_assets_reused': True, 'identity_comparison': 'not_performed', 'motion_continuity_score': None, 'motion_continuity_rating': 'not_evaluated', 'average_displacement_per_prod_frame': 4.82, 'max_displacement_per_prod_frame': 115.01, 'total_production_frames_calculated': 120, 'teleport_threshold_px': 120.0, 'teleport_glitch_detected': False, 'evaluation_verdict': 'NOT_RATED: 변위 통계만 계산했으며 지각 품질 평가는 수행하지 않음'}
keyframes=20 continuous=8
```

## 오디오 WAV 계측 명령

```sh
python3 -c "import wave; p='outbox/benchmark_b_work/corrected_20261107/run_02_remediated/soundtrack.wav'; w=wave.open(p,'rb'); print({'channels':w.getnchannels(),'sample_width_bytes':w.getsampwidth(),'sample_rate_hz':w.getframerate(),'frames':w.getnframes(),'duration_seconds':w.getnframes()/w.getframerate()})"
```

Exit code: `0`; 출력: `{'channels': 1, 'sample_width_bytes': 2, 'sample_rate_hz': 44100, 'frames': 441000, 'duration_seconds': 10.0}`. 청감 평가는 하지 않음.

## SHA-256

명령: `shasum -a 256 <최종 MP4> <초기 MP4>` — exit code `0`.

```text
9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f  final MP4
c4b292c5a9b7db78d2c743c21706c15449c23d9b82c5f6f2733e75d9fe5bc6dc  initial MP4
```

PNG 계수: 최종 Run 2 `frames/` 240장, keyframe 표본 20장, 연속 표본 8장. 동일한 원본 9 레이어 파일도 run 폴더 내에 보존했습니다. `teleport_glitch_detected`는 120px 단일 변위 기준 플래그일 뿐 지각 품질 평가가 아닙니다.
