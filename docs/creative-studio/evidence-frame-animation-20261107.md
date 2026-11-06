# Frame Animation Benchmark B — 구현 증거 및 통합 계약

독립 검증 결과를 반영해 walk/wave 키포즈 위상을 수정하고, 검증되지 않은 품질 점수와 verdict를 제거했습니다. 상세 재현 파일과 실행 기록은 저장소의 [EVIDENCE_INDEX.md](../../outbox/benchmark_b_work/EVIDENCE_INDEX.md), [EXECUTION_LOG.md](../../outbox/benchmark_b_work/corrected_20261107/EXECUTION_LOG.md)에 있습니다.

## 실제 adapter 인터페이스

- `ADAPTER.id == "skill.frame_animation_2d"`
- `preflight() -> Preflight(ok, version, missing)`
- `render(spec: dict[str, Any], *, output_path: Path | str | None = None, workdir: Path | str | None = None) -> dict[str, Any]` — `output_path` 누락 시 `ValueError`
- `render_segment(spec: dict[str, Any], *, output_path: Path | str, workdir: Path | str | None = None) -> dict[str, Any]`
- 입력 키: `seconds` 또는 `target_duration`, `aspect` 또는 `aspect_ratio`, `title` 또는 `segment_id`, `production_fps`, `playback_fps`, `scenes[]` (각 `scene_id`, `seconds` 또는 `duration`, `caption`/`narration`/`title`)
- 반환 키: `output_path`, `sha256`, `duration`, `fps`, `width`, `height`, `engine`, `version`, `real_execution`, `metrics`, `receipt`, `qa`, `motion_eval`

계약이 포함된 메시지를 통합 담당자 `86c93b9b-7d99-4814-b83c-0dd77c453b2c`에게 `message_worker`로 전달하려 했으나, 도구가 `404 not_found: 이 작업 세션의 하위 세션이 아닙니다`를 반환했습니다. 도구의 세션 범위 밖이라 재시도하지 않았습니다. 이 문서와 EVIDENCE_INDEX가 계약 근거입니다. 실제 `engines.py` 등록/dispatch 호출은 통합 담당자 소유이며 여기서 수정하거나 완료로 주장하지 않습니다.

## 검증된 범위와 한계

- 재렌더 실행 명령 exit code `0`; 테스트 `9 passed in 2.73s`; FFmpeg preflight 통과.
- 현재 Run outbox MP4: 10초, 1280×720, 24fps, video/audio 트랙 존재. 기계 QA 6개 항목 통과.
- 새 walk 프리셋 테스트는 사지 각도 폭과 상하 바운스를 검사하고, wave 테스트는 팔 각도가 세 개 이상이며 변화폭을 갖는지 검사합니다. 두 모션의 이전 정지 계산 결함을 수정했습니다.
- 캐릭터 identity는 동일한 레이어 파일을 재사용한 사실만 보고합니다. frame-to-frame 시각 비교는 수행하지 않았습니다.
- 변위 통계는 평균 4.82px/제작 프레임, 최대 115.01px입니다. 연속성 점수/등급과 지각 품질 verdict는 미평가로 `null`/`NOT_RATED`를 출력합니다. 120px 단일 임계치의 teleport 플래그는 자연스러움 판정이 아닙니다.
- Run 1 간극 약 8px는 제작 로그의 추정치이고 픽셀 측정 근거가 없습니다. Run 2에서 피벗 파라미터 변경은 확인되지만 “조인트 100%/0px 해소”는 검증되지 않았습니다.
- 오디오 트랙/음량 수치는 자동 검사했습니다. 청감 평가는 미실행입니다. Studio adapter 등록 및 end-to-end dispatch 통합도 미완료입니다.
