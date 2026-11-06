# HyperFrames 제작 A — 실행 및 품질 증거

기록 시각: 2026-11-07 KST
작업 저장소: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`
범위: `hyperframes.py`, `templates/hyperframes/`, `benchmark_tests/test_hf_minimal/`, `unit_tests/test_hyperframes.py`, 전용 스크립트와 이 문서. `engines.py` / `dispatch.py`는 읽기 전용 대조만 했고 수정하지 않았다. 같은 작업 트리의 타 담당자 변경은 보존했다.

## 1. 재배정 복원과 기존 산출물 확인

- `account-reassignment-20261107.json`에서 HyperFrames 담당 `789099f7-b78c-41e5-b0c1-d8d6b677cc5f`의 실행 중 지시(run `b0e1f732-cc94-4273-97aa-097b124cf72d`)와 대기 지시(run `06ca8007-e295-4ab9-87e9-beb5ca578354`, `d02a89fd-dbd0-4581-b933-b2728661ae6e`)를 복원했다. 원래 요구는 HyperFrames 한영 설명 영상, 실제 렌더 어댑터, core 담당 조기 계약 통보, 렌더·프레임·오디오 증거였다.
- 이미 존재한 `benchmark_tests/test_hf_minimal/out.mp4`와 HyperFrames `render-outputs.json`은 보존했다. 이 파일은 3초 1920×1080 H.264 단일 비디오 스트림이며 오디오가 없다. A 설명 영상으로 간주하지 않았다.
- 기존 성공을 이 세션에서 재현 가능한 범위와 분리했다. 이번 전체 `unit_tests/test_hyperframes.py` 실행은 아래와 같이 통과했다. 실물 16초 A MP4 및 이전 담당자의 렌더 stdout/stderr는 저장소에 없었다.

## 2. 적용한 공식 HyperFrames 규칙

- 사용자 로컬 공식 스킬 스냅샷 `/Users/winterholic/.claude/skills/video-framework/`의 라우터와 `hyperframes`, `hyperframes-core`, `hyperframes-cli`, `faceless-explainer`, `hyperframes-animation`, `hyperframes-creative` 팩을 확인했다.
- 핵심 적용점: root/clip의 `data-*` 타이밍, seek 가능한 단일 paused GSAP timeline, 결정론적 프레임 타이밍, 실제 제작과 검사 분리, `lint` 후 `check`, 미리보기 후 최종 렌더, 최종 품질 `delivery` 사용.
- 실행된 CLI는 `hyperframes 0.9.19`이며 `lint` 결과 `_meta.updateAvailable=false`였다. `render --help`에서 `draft`, `looks`, `delivery`, `standard/high`가 지원됨을 확인했고 어댑터 기본 제작 옵션을 `delivery`로 맞췄다.
- 이번에는 browser preflight가 차단되어 `check`, `preview`, `snapshot`, 최종 render 순서를 완료하지 못했다. 실제 렌더를 완료했다고 주장하지 않는다.

## 3. 제작 소스와 입력

- 컴포지션/GSAP 소스: `templates/hyperframes/kafka_message_flow/index.html`
- 제공 미디어: `templates/hyperframes/kafka_message_flow/audio.wav`
- 실제 입력 spec: `templates/hyperframes/kafka_message_flow/input_spec.json`
- 스토리보드: `templates/hyperframes/kafka_message_flow/STORYBOARD.md`
- 재현 스크립트: `scripts/render_hyperframes_benchmark.py`
- 재사용 모듈: `src/ttakkari/creative/v2/hyperframes.py`

입력은 16초, 1920×1080, 30fps, 16:9이며 Producer → Broker → Consumer 세 장면을 한영 자막으로 정의한다. `render(spec, output_path, workdir)`는 제목, 각 장면의 제목/설명, 전체 초 길이, 해상도, fps 및 선택 음악 파일을 작업용 HyperFrames 프로젝트로 옮긴다. style 값은 템플릿의 고정 설정과 일치하는 값만 받고, 카메라·샷·에셋 필드는 `stage=prepare`로 명시 거부한다. 기존 workdir 또는 출력 파일이 있으면 덮어쓰지 않는다.

정확한 추후 렌더 명령(현재 Run outbox 전용 경로; **이번에는 실행하지 않음**):

```sh
UV_CACHE_DIR=/private/tmp/codex-uv-cache npm_config_cache=/private/tmp/codex-npm-cache PYTHONPATH=. uv run python scripts/render_hyperframes_benchmark.py \
  --output /Volumes/Backup/ttakkari-data/outbox/5286b1cd-187b-4743-97a1-4e3f79cc4d89/hyperframes_kafka_explainer.mp4 \
  --workdir /Volumes/Backup/ttakkari-data/outbox/5286b1cd-187b-4743-97a1-4e3f79cc4d89/hyperframes_kafka_explainer_work
```

## 4. 실행 관측

### 브라우저 사전 점검

실행 명령:

```sh
/Users/winterholic/.ttakkari/deploy/releases/efe07c4bf9332473c60743bf31bb73111be00c4a/.venv/bin/python /Users/winterholic/.ttakkari/deploy/releases/efe07c4bf9332473c60743bf31bb73111be00c4a/src/ttakkari/agents/browser_preflight.py
```

관측: exit 78, `status=blocked`, `browser_launched=false`; Mach `mach-register`와 두 `mach-lookup` 검사에서 `denied`. 직접 Chrome 렌더는 시작하지 않았다. 승인된 browser MCP는 `file:` 프로토콜 접근에서 `Access to "file:" protocol is blocked`를 반환했다. 추가 브라우저/로컬 서버 시도는 하지 않았다.

### spec 적용 프로젝트 lint/timeline

변환 프로젝트는 `/private/var/folders/.../hf-a-20261107-final-4whu6lhe/project` 임시 경로에 준비했다. 공식 CLI 명령:

```sh
HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_SKIP_SKILLS=1 npm_config_cache=/private/tmp/codex-npm-cache npx -y hyperframes lint --json
npx -y hyperframes timeline --json
```

관측: `lint ok=true`, 오류 0, 경고 3, HyperFrames 0.9.19. 경고는 머신 로컬 폰트만 지정한 점 1건과 clip 내부 중첩 노드 2건이다. `timeline`은 16초 composition, 배경/스테이지 graphics clip 두 개와 16초 audio track 한 개를 표시했다.

### 모듈 연결 계약

관측 명령:

```sh
PYTHONPATH=src uv run python -c 'from ttakkari.creative.v2 import engines; print([(a.id, callable(getattr(a,"render_segment",None))) for a in engines.load_adapters()])'
```

출력: `('skill.hyperframes', True)`가 `engines.load_adapters()`에 등록됐다. 등록 관측은 스튜디오 dispatch로 실제 제작 성공을 의미하지 않는다. `engines.py`/`dispatch.py` 소유자와의 연결은 이 파일/공통 계약 문서가 기준이다. 작업 카드에 기록된 형제 worker `message_worker`의 404 연결 실패는 재호출하지 않았고, 이번 담당은 공유 저장소 계약 문서와 이 증거를 남겨 총괄에 보고한다.

### 테스트 및 spec 반례

```sh
UV_CACHE_DIR=/private/tmp/codex-uv-cache npm_config_cache=/private/tmp/codex-npm-cache PYTHONPATH=. uv run pytest unit_tests/test_hyperframes.py -q
```

관측: `9 passed in 0.85s`. spec이 제목/장면 글/해상도/시간을 바꾸는 테스트, 렌더 전 실패 반환, 소비하지 않는 style 거부, browser IPC 차단 때 CLI doctor를 실행하지 않는 검사를 포함한다. 콘텐츠 적용 테스트는 구현 전에 `AttributeError: module ... has no attribute 'render'`로 실패한 뒤 수정 후 통과했다.

## 5. 오디오/비디오 검사 범위

- 입력 WAV 측정: PCM signed 16-bit, mono, 44.1 kHz, 16.0초, peak `-27.72 dBFS`, RMS `-36.79 dBFS`.
- 기존 최소 MP4 `ffprobe`: 3.0초, H.264, 1920×1080, 비디오 1 stream, 오디오 stream 없음. 이는 A 영상이 아니다.
- 입력 텍스트는 spec을 변환한 HTML과 lint 대상에서 확인했으며, 코드상 위계는 38px/800 제목, 20px/700 캡션 제목, 16px 설명이다. 이 관찰은 렌더 프레임의 가독성 판정이 아니다.
- 소스에는 producer→partition, broker append/replication, consumer offset 진행의 GSAP 모션이 정의되어 있다. 실제 출력의 프레임별 변화와 연속 프레임 품질은 브라우저가 열리지 않아 검수하지 않았다.
- WAV 원본 수치만 측정했다. 최종 MP4 audio track/loudness, 음질·음악 적합성 실제 청취는 미검증이다.

## 6. 실패 경로와 남은 결함

- browser IPC가 `blocked`/`unknown`이면 HyperFrames 모듈 preflight는 그 상태를 `missing`에 기록하고 `npx doctor`/render를 호출하지 않도록 보완했다. renderer는 `success=false`, `stage=preflight|lint|prepare|render`, 원인을 반환하며 묵시적 슬라이드쇼 대체를 하지 않는다.
- style 입력 반례 작성 중, 새 코드가 적용되기 전 한 차례 실제 render 분기가 호출되었다. 반환 단계는 `stage=render`, `out.mp4` 없음으로 확인했다. 이 pytest 출력에는 CLI stderr 원문이 포함되지 않아 실패 세부 원인은 미확인이다. 사용자 제한을 따라 재현하지 않았다. 해당 반례는 현재 `theme=light`를 `stage=prepare`에서 거부한다.
- 정적 lint의 3개 경고(로컬 전용 폰트, 중첩 clip 구조)는 남아 있다. 실제 프레임을 보지 못해 visual-risk를 단정하거나 구조를 재설계하지 않았다.
- `hyperframes check`, preview/snapshot, 완성 A MP4, rendered MP4의 ffprobe/프레임 추출/연속 프레임/오디오 QA 및 실제 청취는 하지 않았다. 이유: 실행 환경 browser preflight 차단 및 승인 브라우저의 `file:` 접근 거부.
- 완료 MP4 공유 ID: 없음. MP4가 생성되지 않아 파일 공유를 하지 않았다. 이 증거 문서의 공유 ID는 총괄 공유 후 여기에 기록한다.
- 현재 Run 공유 파일 ID: 증거 문서 `67ec59a3-2e8b-4726-a0e6-c72e5ab62b8f`(최초 공유본), 소스 아카이브 `a21158b3-9817-46a5-a6d3-5ffa5da3307c`. MP4 ID는 없음.

## 7. 작업 트리 경계

`engines.py`, `dispatch.py`와 타 담당자 변경은 수정하지 않았다. `git diff --check`는 출력 없이 exit 0이었다. commit/push는 하지 않았다.

## 8. 광고 지원 범위 교정 및 Core dispatch 계약 대조 (2026-11-07)

### 변경 파일과 확정된 모듈 입력 범위

- 변경 파일: `src/ttakkari/creative/v2/hyperframes.py`, `unit_tests/test_hyperframes.py`, 이 문서. 템플릿/스크립트에는 변경 없음.
- `HyperFramesAdapter.limits`는 템플릿이 실제 처리하는 `aspects=("16:9",)`, `min_scenes=max_scenes=3`을 광고한다. 프로젝트 전체 길이는 1~300초다. `max_scene_seconds=60`은 현재 Core 쪽 장면 길이 guard이며, 템플릿은 장면별 길이·시작/끝 값을 읽지 않고 내장된 16초 타임라인을 전체 길이에 맞춰 스케일한다. 즉, 장면별 시간 배치는 입력 계약상 지원되지 않는다.
- `style_params`는 템플릿이 받는 고정값만 공개한다: `theme=dark`, `animation=technical`, `transition=cut`, `caption_style=bottom_card`. 다른 열거값을 실제로 매핑하는 템플릿 로직은 없다.
- 모듈 입력은 3개 object 장면이며 각 장면에 비어 있지 않은 `title` 또는 `caption`, 그리고 `narration`/`caption`/`prompt` 중 하나가 있어야 한다. 16:9가 아니면 준비 단계에서 거부한다. `style`은 위 고정값만 허용한다. Core `ComposeResult` 모양의 `style_dna`는 `resolved` 또는 `resolved_pending_confirmation` 상태만 받는다. DNA의 `visual_identity.params.theme/caption_style`과 `motion_language.params.animation/transition`을 템플릿 고정값과 대조한다. 값이 다르면 경로·값·지원값을 포함해 `stage=prepare`에서 거부한다. 일치 값은 템플릿 값을 바꾸는 것이 아니라 기존 고정 설정과 일치함을 확인한다.
- 지원 매개변수 외 DNA 값(예: facet prose, palette, lighting, camera_language, quality_constraints)은 소비하지 않는다. 모듈의 `validate_style_dna()` 반환값과 성공 receipt의 `style_dna_contract.unapplied_fields`에 경로와 원래 값을 남긴다. 이는 전체 Style DNA가 적용됐다는 뜻이 아니다. 기존 workdir의 `project/index.html`이나 기존 출력 파일도 덮어쓰지 않는다.
- 각 scene의 `seconds`, `start_time`, `end_time` 등 타이밍 메타데이터는 현재 입력 검사에서 거부되지는 않지만 템플릿에는 적용되지 않는다. 합성 타이밍은 내장 timeline에서 오므로 실제 적용 요청은 전체 `seconds` 하나로 한정한다.
- 이 계약은 단일 Kafka flow 템플릿의 범위다. HyperFrames 일반 엔진의 여러 비율/임의 장면/가변 스타일 지원을 의미하지 않는다.

### Core가 넘기는 값과 실제 dispatch 차단점

`src/ttakkari/creative/v2/engines.py`, `dispatch.py`, `plans.py`는 수정 없이 읽기 전용으로 확인했다.

1. `engines.ModuleAdapter.validate_spec()`는 HyperFrames 장면 각각에 `preset="kafka_flow"`가 있고 구간이 정확히 3장면일 것을 요구한다. `plans._scene_records()`가 만드는 Core 장면에는 `preset` 필드가 없다. 따라서 현재 계약 그대로면 모듈의 `render_segment()`를 부르기 전에 `ValueError`로 막힌다. 이는 코드 경로 대조 결과이며 실제 Studio dispatch 실행 증거는 아니다.
2. `plans.render_plan()`은 비감독 경로에서 구간당 최대 40장면을 묶고, 감독 경로는 `code_motion`끼리 최대 3장면을 묶는다. `ModuleAdapter`의 3장면 검사는 뒤 경로에서만 수가 우연히 맞을 수 있으며, 일반적인 3~40장면 구간은 모듈의 정확히 3장면 제한과 다르다. 장면이 1~2개인 감독 구간 역시 거부된다.
3. `dispatch._render_producer()`는 Core `ComposeResult`인 `style_dna=plan.get("style")`와 `editorial_scenes`를 전달한다. 이제 모듈은 Style DNA를 일괄 거부하지 않고 앞서 적은 네 고정 매개변수 경로를 검증하며 나머지 값은 경로·원값을 `unapplied_fields`로 보고한다. Core `_style_defaults()`는 `animation`, `transition`, `caption_style` 세 입력만 선택값으로 다루고 `theme`는 `STYLE_SOURCES`에 없다. 현재 wrapper는 모듈의 새 `validate_style_dna()`를 호출하지 않으므로, 렌더 진입 전 선검증·미적용 값 노출은 아직 Core에서 연결되지 않았다.
4. Core 장면에는 `strategy`, `shots`, `camera`, `motion` 등 제작 메타데이터가 있을 수 있다. wrapper는 임의 `required_capabilities`와 custom `camera`/`motion`을 차단하고, 모듈은 위의 truthy 장면 필드를 소비하지 않는 입력으로 거부한다. `title`/`narration`만으로 구성된 소유 Kafka 입력 spec은 이 메타데이터를 포함하지 않는다.
5. Core 지원 비율에는 9:16 등이 들어올 수 있지만 HyperFrames Kafka 템플릿은 16:9만 허용한다. 이제 어댑터 limits가 이를 광고하지 않으므로 일반 경로의 discovery/dispatch preflight가 거부해야 한다. 감독 경로에서도 선택 어댑터의 제한 검사가 적용된다.

통합 가능한 현 계약은 **3장면·16:9·고정 Kafka flow**이며, Core 통합 소유자가 preset 규칙, 장면 구간화, 편집 메타데이터 제거/전달, 스타일 사전 검증을 함께 조정하기 전에는 Studio dispatch 성공으로 간주할 수 없다. 본 변경은 `ModuleAdapter`의 기존 scene-count/preset 검증을 복제하거나 수정하지 않았다.

### Core 담당자 이어받기 — 미해결 항목

1. Core 담당자가 `engines.ModuleAdapter`에 delegate `validate_style_dna()` 위임을 연결한다. 기존 Kafka preset 및 3장면 검증은 그대로 두고 이를 복제하지 않는다.
2. candidate engine을 확정하거나 dispatch하기 전에 해당 validator를 실행한다. 지원 경로의 값이 아니면 반환된 경로·실제값·허용값으로 계획 선택을 막고 사용자에게 수정 가능한 사유를 노출한다.
3. validator의 `unapplied_fields` 경로/원값을 선택 UI 또는 dispatch 전 검토에 드러내고, Style DNA 전체를 적용한 것으로 표시하지 않는다. Core `_style_defaults()`의 `theme` 누락도 정리한다. 미적용 필드를 허용할지 차단할지는 Core 통합 계약에서 명시한다.
4. 원래 `style_dna`가 수정 없이 모듈에 도달하는 경우 module receipt에는 `status=partial_match`, `sha256`, 고정 매핑 일치값, 미적용 경로·원값이 기록된다. Core 사전 검증 연결 이후 승인된 Studio 경로에서 이 계약과 실패 사유가 보존되는지 별도로 확인한다.

### 좁은 검증 기록

- TDD RED: `UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=src uv run pytest unit_tests/test_hyperframes.py -q -k 'protocol_conformance or outside_fixed_template or portrait_before_preflight or discovery_only_style_values'` → `1 failed, 7 passed, 8 deselected`; 실패는 기존 discovery가 `min_scenes=1`, `max_scenes=60`, 9:16/1:1을 광고한 assertion이었다.
- GREEN: 같은 명령 재실행 → `8 passed, 8 deselected in 0.08s`. 검증에는 `engines.adapter_by_id()`를 통한 wrapper discovery, 1/4장면, 9:16 및 과거 advertised style 값 4종의 준비 단계 거부가 포함된다. 거부 반례는 브라우저 preflight와 렌더를 호출하지 않는 것을 테스트에서 고정했다.
- `git diff --check` → 출력 없음, exit 0.
- 기존 증거 재사용: 앞선 Run5286b1cd events seq135의 `9 passed in 0.85s`, seq139의 lint `ok`, 오류 0/경고 3, seq141의 16초 timeline, seq29의 browser preflight exit 78(`blocked`, `browser_launched=false`). 이 검증에서는 다시 실행하지 않았다.
- 본 변경으로 실제 MP4, 최종 프레임, 최종 오디오/청취, HyperFrames Studio dispatch는 수행하지 않았다. 지원 실행 경로에서만 사전 점검을 통과한 뒤 소유 스크립트로 A를 렌더하고, HyperFrames `check`/preview/snapshot 및 delivery 렌더, 출력 MP4 ffprobe, 장면별 최종 프레임·연속 프레임 변화, 오디오 트랙/loudness와 실제 청취를 별도 확인해야 한다. Studio 통합은 위 Core 입력 계약 차이를 해소하고 승인된 dispatch 경로에서 성공 receipt와 최종 산출물을 확인해야 한다. 브라우저 차단을 다른 엔진·바이너리·설정으로 우회하거나 이 Run에서 MP4를 재시도하지 않는다.
- 후속 Style DNA 모듈 계약 검증: `UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=src uv run pytest unit_tests/test_hyperframes.py -q -k 'core_style_dna or unsupported_core_value'` → RED `3 failed`(truthy Core DNA 일괄 거부, 사전검증 메서드 부재, 잘못된 animation 경로를 특정하지 못함), 구현 후 GREEN `3 passed, 16 deselected in 0.07s`. 테스트는 Core `_render_producer` spec의 상위 필드와 `editorial_scenes` 모양, 지원값 매핑, 미적용 원값 receipt, `animation=pop`의 preflight 전 거부를 확인한다.
- 기존 discovery/preparation 반례와 신규 Style DNA 계약을 함께 확인: `UV_CACHE_DIR=/private/tmp/codex-uv-cache PYTHONPATH=src uv run pytest unit_tests/test_hyperframes.py -q -k 'protocol_conformance or outside_fixed_template or portrait_before_preflight or discovery_only_style_values or core_style_dna or unsupported_core_value'` → `11 passed, 8 deselected in 0.20s`.
