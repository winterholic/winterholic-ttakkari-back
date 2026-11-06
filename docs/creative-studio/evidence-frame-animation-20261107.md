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

## 2026-11-07 평가 보고 코드 보완

- `_build_motion_eval()`은 레이어 자산 재사용 여부와 root 변위 휴리스틱만 기록합니다. 변위가 임계치를 넘으면 `teleport_glitch_detected`를 표시하지만, 모든 변위에서 연속성 점수는 `null`, 등급은 `not_evaluated`, 품질 verdict는 `NOT_RATED`입니다. 외부 시각 평가는 수행하지 않았습니다.
- CharacterLayerBuilder 설명에서 렌더 시각 품질 보장 표현을 제거했습니다. 벤치마크 실행 메시지도 피벗 변경 사실과 시각 개선 미검증을 분리했고, JSON 보고서의 중복 `motion_eval` 키를 제거했습니다.
- 고변위(180px) 음성 반례 회귀 테스트와 프레임 애니메이션 테스트 8개, 벤치마크 안전성 테스트를 통과했습니다. 픽셀 비교·시청 품질 판정·청감 평가는 여전히 미실행입니다.

## 2026-11-07 긴 포즈 보간 교정 및 실제 영상 확인

- 시작 기준은 직전 독립 검증이 기록한 dirty 소스 해시(frame_animation.py `942e97736afc2b8267ade3ad3f42c8ae0e634cb4ecc0ab57e334879276c60bb6`, benchmark script `e58e3b1ae48c4d4d842cfd34b3ba40cd334c4e40e40a437e7b2982d35a532baa`, unit tests `b29d46c7ec5b6a61e048acf06c2dfd77d26cd30503b79d8f2f06b0e4007510a6`)와 수정 전 비교 MP4 SHA `c4b292c5…`/`9719e238…`이다. 해당 두 저장소 MP4는 보존했고 해시가 계속 일치한다.
- `hold`는 기본 동작으로 보존했다. B storyboard만 `linear`를 사용하고 12fps 키포즈 사이 수치 관절/루트 변환을 24fps 재생 시각에서 보간한다. 범주형 표정·레이어 변형은 직전 키포즈 값을 유지하다 키포즈에서 바뀐다. Optical Flow는 쓰지 않는다.
- 기준 hold와 수정 linear의 같은 steps=2/waves=2 입력으로 각각 10초, 1280×720, 24fps MP4를 만들었다. 9개 캐릭터 레이어 PNG, soundtrack.wav(SHA `5a354705…`), scene_03_jump 입력 구조가 이전 corrected Run 2와 일치한다. 각 영상의 실제 MP4를 디코드해 240프레임을 확인했다.
- 구조화된 변환 측정(영상 프레임의 관절을 추정한 값이 아님): 걷기 1.25/1.50/1.75초 왼쪽 다리 회전은 hold `28/28/28°`, linear `21/14/7°`; 인사 4.5/4.6/4.8/5.0초 왼팔은 hold `−140°` 고정, linear `−136.154/−132.308/−124.615/−116.923°`다. 같은 시각 카메라 팬·줌도 별도 기록했다.
- 같은 시각 표본 접촉 시트와 1초 연속 MP4 프레임(걷기·인사)을 직접 확인했다. 표본에서는 사지 포즈가 연속적으로 달라지고 자막은 읽히며 프레임에서 잘리지 않았고 뚜렷한 어깨/사지 틈은 보이지 않았다. 이 표본 관찰은 전 프레임 자연스러움이나 간극 픽셀 측정이 아니다. 템포의 지각 평가는 미평가다.
- 이전 수정 영상 SHA `9719e238…`과 새 영상도 실제 MP4에서 디코드해 11개 동일 시각 접촉 시트로 직접 비교했다. 같은 입력 hold 대 linear 비교 시트도 별도 제공한다. 샘플 간 움직임 차이는 카메라 확대/팬과 포즈 변경을 분리한 구조값 trace와 함께 본다.
- 자동 Markdown 보고서의 `True`는 **기계 미디어 QA 판정**임을 명시하고 시청/청감 평가를 별도로 미평가로 표시한다. 고정 품질 점수나 자동 시청 PASS는 없다. 음량 측정은 청취 판정으로 취급하지 않는다.
- 실행 명령, 산출물·소스 SHA-256, decoded-frame 카운트, 육안 표본 한계는 이번 Run outbox의 `benchmark_b_execution_evidence.md`와 `benchmark_b_execution_evidence.json`에 기록했다. 새 MP4와 같은 입력 hold 참조 영상은 기존 비교본을 덮어쓰지 않고 새 이름으로 저장했다.
- 좁은 회귀: 프레임 애니메이션 unit tests `9 passed`; 변경 파일 `py_compile` 성공. Studio 등록/dispatch, 전체 프로젝트 회귀, 전체 길이의 지각 품질과 청감은 이번 B 범위/완료 항목이 아니므로 재판정하지 않았다.
