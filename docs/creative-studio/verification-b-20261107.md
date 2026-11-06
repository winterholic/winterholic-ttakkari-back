# Frame Animation B 독립 검증 — 2026-11-07

## 판정

**전체: 부분 통과.** 독립 렌더, 10초 MP4, 스트림·프레임 타이밍, 기계 QA, 샘플 이미지 확인은 확보했다. 하지만 걷기와 손 흔들기 프리셋이 계산상 거의 고정 포즈라 제작 동작 품질 주장은 **실패**다. 스튜디오 레지스트리에 아직 등록되지 않아 호출 가능성은 **통합 대기**로 분류한다. 오디오 청취 품질은 **미검증**이다.

검사 시간: 2026-11-07 00:15:21–00:20:02 KST
검사 저장소: /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107
브랜치 / revision: feat/creative-production-20261107 / 5f5cbd997addcb4ea084ab908790f8f6eec54122

## 1. 입력·저장소 상태

검사 시작 시 구현/테스트/벤치마크 파일은 커밋 diff가 아니라 untracked 상태였다. 검사 시작 시점 dirty 목록:

- bench/scene_3d_blender/, benchmark_tests/test_hf_minimal/
- docs/creative-studio/engine-contract-20261107.md, docs/creative-studio/scene-3d-module-spec.md
- outbox/benchmark_b_work/
- scripts/render_3d_benchmark.py, scripts/run_frame_animation_benchmark.py
- src/ttakkari/creative/v2/frame_animation.py, hyperframes.py, scene_3d.py
- templates/, tests/test_scene_3d.py
- unit_tests/test_creative_v2_frame_animation.py, test_hyperframes.py, test_scene_3d.py

검사 중 다른 작업의 파일 변경도 나타나 asset_generation.py, asset_generation_managed.py는 modified로 보였다. 초기 검사 중 account-reassignment-20261107.json, evidence-frame-animation-20261107.md가 추가됐고 종료 점검 때 account-reassignment-receipts-20261107.json, worker-owners-20261107.json, quality.py, quality 테스트, benchmark safety 테스트도 추가로 나타났다. 이 항목은 건드리지 않았다. 대상 입력 해시는 검사 후에도 시작 때 기록한 값과 같았다.

| 입력 | SHA-256 |
|---|---|
| AGENTS.md | b91bbb8e7b5b610682789000d6a9f974719f7093b199cf4c83003b8dbf634477 |
| src/ttakkari/creative/v2/frame_animation.py | a21c6826ef4c827c3ea96eafabc25ea9c178124c44165ab801adcefc48b176cb |
| unit_tests/test_creative_v2_frame_animation.py | 89f4ddf64ab7f56d95e73a712bd797e062913cfcea455f96b37a8a350da58321 |
| scripts/run_frame_animation_benchmark.py | ad4e6d847e45f843c78e32a8ce0619594161de10c4bc73b253ba18b14e5f892d |
| outbox/benchmark_b_work/BENCHMARK_B_REPORT.json | 7ad6a0f10ad5bd5aefaaa291f013b1bbed82dd7f60be3ceff549c3fcbd7b7799 |
| docs/creative-studio/engine-contract-20261107.md | a482d9faea1bf859b1d528f39a1cef15aeb56ca6bac66e628d7193372d0fd5c4 |

## 2. 단위 테스트와 독립 렌더

- 요청한 uv run pytest unit_tests/test_creative_v2_frame_animation.py -q는 uv 기본 캐시 /Users/winterholic/.cache/uv 접근 거부로 시작하지 못했다.
- 캐시를 허용된 임시 경로로 옮긴 뒤 PYTHONPATH=.:src UV_CACHE_DIR=/private/tmp/uv-cache-b uv run pytest unit_tests/test_creative_v2_frame_animation.py -q 실행 결과 **5 passed in 2.84s**. PYTHONPATH 없이 재시도했을 때는 수집 중 ModuleNotFoundError: No module named 'templates'가 발생했다.
- FrameAnimationAdapter.preflight() 출력: ok=true, ffmpeg version 7.1, 누락 항목 없음.
- 실제 FrameAnimationAdapter.render_segment()를 직접 호출해 내 Run outbox에 MP4를 생성했다. 입력은 10초, 16:9, 제작 12fps/재생 24fps, 4초 걷기·3초 인사·3초 점프와 한국어 자막 세 개다.
- 렌더 반환: duration=10.0, 1280x720, fps=24, total_frames=240, 기계 qa.passed=true; SHA-256 ff85472793fb61201ff9f3a000aed2876af6aef44f469f22f984c80603aabe4f.

벤치마크 스크립트는 실행하지 않았다. 코드가 실행 시 다른 Run outbox(83968b68-..., 20d17a92-...) 경로를 rmtree 후 복사하도록 되어 있어 이 검사 범위 및 금지와 충돌한다. 저장소의 기존 outbox/benchmark_b_work 원본은 읽기만 했다.

## 3. MP4·키포즈·시각·음향

- ffprobe 요약에서 컨테이너 길이 10초, H.264 1280×720 영상과 AAC 오디오 각 10초를 확인했다. 이 환경의 /Users/winterholic/bin/ffprobe는 -show_entries 선택 결과를 출력하지 않는 요약 wrapper여서 FPS/프레임 행은 제공되지 않았다.
- 독립 확인은 ffmpeg -vf showinfo 디코드로 했다: **240 프레임**, 처음 0.000000, 0.0416667, 0.0833333, 0.125, 마지막 9.833333 … 9.958333; 간격은 약 1/24초, 누락·긴 간격 0건. 렌더된 원본 PNG도 재생 프레임 240개이며, 12fps 제작 프레임을 24fps로 재생할 때의 **120/120 쌍이 같은 이미지**라 hold 방식이 실제 산출물에서 확인된다.
- 렌더 manifest는 20개 키프레임 샘플(0.5초 간격)과 1.0–1.583초 연속 샘플 8개를 기록한다. 장면 길이 4/3/3초 합과 총 10초는 맞다.
- 접촉 시트와 4.5초 원본 프레임을 육안 확인했다. 인사/점프 포즈와 화면 확대는 보이며, 샘플 자막은 한국어로 읽히고 잘림·가짜 문자는 보이지 않았다. 샘플 어깨 연결부에 뚜렷한 틈은 보이지 않지만, 기존 보고의 “약 8px 결함 → 100% 수정”은 Run 1/Run 2 동일 시점의 픽셀 비교·측정 자료가 없어 독립 입증하지 못했다.
- 동작 품질 **실패 — 재현됨**. 어댑터 기본 설정의 make_walk_cycle_poses(0, 4, steps=2, ...)는 5개 키포즈에서 최대 사지 회전 1.5675e-14°, 최대 수직 바운스 6.8580e-15px를 낸다. 위상 iπ에 sin()을 넣어 사지 회전과 바운스가 사실상 0이 되는 것이 수치 원인이다. make_wave_poses(0, 3, waves=2, ...)도 4개의 swing 포즈 팔 각도가 전부 -140°여서 손 흔들기가 없다. 접촉 시트의 1.0–1.583초 프레임은 이 정지에 가까운 걷기를 시각적으로 확인시킨다. 점프 포즈는 표본에서 팔 동작은 보이나 전체 프레임의 자연스러움까지 통과라고 하기는 어렵다.
- character_identity_consistency=1.0은 실제 프레임 비교값이 아니다. render_project()가 상수 1.0을 기록하고 벤치마크 스크립트도 consistency_rate: 1.0을 고정 입력한다.
- motion_continuity_score=0.98은 영상 평가 결과가 아니다. 최대 변위가 80px 미만이면 부여하는 조건식의 값이다. 본 독립 렌더에서는 평균 변위 4.58px, 최대 115px여서 점수는 0.85였지만 evaluation_verdict는 문자열 상수 PASS였다. 이 verdict도 측정 결과와 일치하지 않는다.
- 오디오 기계 측정은 mean -14.5 dB, peak -1.7 dB, 트랙 존재 확인이다. 실제로 들어보는 청취 도구가 없어 음질·음악의 자연스러움은 **미검증**이다.

## 4. 어댑터 계약과 스튜디오 연결

- FrameAnimationAdapter는 문서 계약의 id, providers, limits, style_params, preflight(), render_segment(spec, *, output_path, workdir)를 제공한다. 모듈의 ADAPTER, preflight(), render_segment() 노출도 있다. 이 검사에서는 preflight와 render 함수를 실제 호출했다.
- **등록은 아직 안 됨.** 현재 engines.ADAPTERS는 skill.v1_ffmpeg_slideshow 하나뿐이며, engines.adapter_for({'skill.frame_animation_2d'}) 결과는 None이다. dispatch.py는 현재 엔진이 v1 slideshow뿐이라고 명시하고, 계획 dispatch 뒤 studio.enqueue()로 연결한다. FrameAnimationAdapter를 선택해 render_segment()까지 보내는 스튜디오 통합 호출은 확인되지 않았다.
- 따라서 어댑터 모듈의 계약 구현/직접 실행은 통과, 엔진 등록은 미완, 스튜디오 호출·통합은 **통합 대기**다. 작업 카드가 알린 core 통합 담당자의 진행 중 연결은 이 상태로 분리 기록한다.

## 결과 분류 및 다음 조치

| 분류 | 결과 |
|---|---|
| 통과 | 테스트 5건, preflight, 독립 MP4 생성, 10초/해상도/스트림, 24fps 프레임 연속성·12→24 hold, 샘플 자막 가독성 |
| 부분 통과 | 팔 관절 샘플, 카메라 연출, 점프 포즈, 기계 오디오 트랙·음량 |
| 실패 | 걷기 키포즈 사지·바운스가 수치상 0, 인사 손 흔들기 팔 각도 고정; 시각 동작 품질 점수 주장 근거 없음 |
| 미검증 | 실제 청취 품질, 전 프레임 자연스러움, Run 1→2 관절 간격 개선의 픽셀 단위 비교 |
| 통합 대기 | engines.ADAPTERS 등록, 제작 계획 dispatch에서 FrameAnimationAdapter 실제 호출 및 산출 연결 |

이 감사 세션은 구현 파일을 수정하지 않았다. 걷기/인사 모션 프리셋의 위상 계산을 바로잡고, 상수 점수·무조건 PASS verdict를 실제 측정과 분리하는 수정이 필요하다. 이어서 core 등록 및 스튜디오 dispatch 통합 뒤 실제 계획에서 end-to-end 호출을 확인해야 한다.

## 산출물

- 독립 MP4: frame-animation-b-independent.mp4 (10초, H.264/AAC)
- 키프레임 접촉 시트: independent-contact-sheet.jpg
- 연속 프레임 접촉 시트: continuous-contact-sheet.jpg
- 독립 MP4 SHA-256: ff85472793fb61201ff9f3a000aed2876af6aef44f469f22f984c80603aabe4f


---

## 후속 독립 재검증 — 수정 MP4·모션·미평가 표기 (2026-11-07 01:00–01:11 KST)

이 절은 위의 이전 검증 기록을 삭제하거나 소급 수정하지 않는다. 아래 결과가 5f5cbd9 시점의 걷기/인사 실패와 점수 과장 판정을 갱신한다.

### 기준 revision과 검사 범위

- 검사 시각: 2026-11-07 01:00:34 KST부터 약 01:08 KST까지.
- HEAD: 439472b3bc4665edc04ad1d7fa8ad1a9684a47b4, 메시지 fix frame animation motion and evidence.
- B 소유 dirty 파일 3개(이 worker는 중지됐다고 전달받음):

| 파일 | SHA-256 |
|---|---|
| src/ttakkari/creative/v2/frame_animation.py | 942e97736afc2b8267ade3ad3f42c8ae0e634cb4ecc0ab57e334879276c60bb6 |
| scripts/run_frame_animation_benchmark.py | e58e3b1ae48c4d4d842cfd34b3ba40cd334c4e40e40a437e7b2982d35a532baa |
| unit_tests/test_creative_v2_frame_animation.py | b29d46c7ec5b6a61e048acf06c2dfd77d26cd30503b79d8f2f06b0e4007510a6 |

- 프리셋은 HEAD에서 수정됐고 templates/frame_animation/presets.py SHA-256은 48a5423aa4bfad3a15e94a088df5b803f6fe95708803facc72cd8b91356a1fa8이다. 현재 프리셋은 walk phase와 wave swing phase에 iπ/2를 사용한다.
- B 바깥의 core 파일도 dirty 상태(src/ttakkari/api/creative_v2.py, creative/v2/asset_generation*.py, dispatch.py, engines.py, plans.py 등)였으며 이 검사에서 건드리거나 통합 상태를 재판정하지 않았다. 이 문서의 모션 판정과 스튜디오 통합 상태는 별개다. 통합은 core 변경 진행 중이라 이번 검사에서 미검증이며, 앞 절의 과거 등록 관찰을 현재 상태로 가정하지 않는다.

### 원래 실패 설정과 기본 벤치마크의 수치·영상 판정

수정 전 실패 조건이었던 steps=2는 실제 기본 benchmark spec에도 그대로 사용된다. 수정된 preset에서 steps=2는 키포즈 시각 0/1/2/3/4초, 왼쪽 다리 회전 0/28/0/−28/0도, 왼쪽 팔 0/−32/0/32/0도, root_y 0/−14/0/−14/0px를 만든다. 따라서 반대 위상 사지와 14px 범위의 수직 바운스가 생긴다. waves=2의 테스트 입력은 6개 포즈에서 팔 각도 −20, −140, −115, −140, −165, −140도이고, 실제 steps=2/waves=2 렌더의 팔 각도도 이 시퀀스로 확인됐다.

기본 benchmark 설정은 walk steps=2, wave waves=3, jump 3초이며 렌더 시각에 손 각도가 실제로 바뀐다. 수정 기본 벤치마크 렌더는 원본 MP4와 spec이 같았고 240개 PNG 입력 프레임도 각각 바이트 동일했다.

실제 디코드 MP4를 육안 확인한 결과 걷기 사지와 인사 팔 동작은 존재한다. 다만 hold 때문에 walk 키포즈가 1초씩 고정되고, waves=2 인사는 약 0.9초마다 팔 각도가 바뀐다. 카메라 이동과 동작 변화가 동시에 보이므로 일부 구간만 재생하면 사지 정지 상태에서 카메라/타임라인만 움직이는 구간으로 보일 수 있다. 구간 전체에서는 다리·팔 변화가 확인되지만, 속도가 느리고 pose hold가 길어 자연스러움/완성도 평가는 통과가 아니라 **미평가**로 둔다.

24개 선택 프레임(실제 MP4 디코드)에서 어깨·팔과 다리에 눈에 띄는 분리 틈은 없었고, 표본 자막은 잘리지 않으며 읽을 수 있었다. 이 샘플 육안 확인은 Run 1의 약 8px 추정 간극을 픽셀 측정하거나 -36px와 -24px를 정량 비교한 결과가 아니다. 카메라 팬/줌은 소스 키프레임과 프레임에서 확인했다. 전체 240프레임의 지각 품질은 평가하지 않았다.

### 수정본 MP4와 소스·비교본의 관계

- 저장소 corrected Run 1 초기 비교 MP4: outbox/benchmark_b_work/corrected_20261107/run_01_initial/animation_run01_initial.mp4, SHA-256 c4b292c5a9b7db78d2c743c21706c15449c23d9b82c5f6f2733e75d9fe5bc6dc.
- 저장소 corrected Run 2 수정 MP4: outbox/benchmark_b_work/corrected_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4, SHA-256 9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f.
- 현재 HEAD+dirty 소스의 build_benchmark_spec 및 FrameAnimationRenderer(-24px)로 Run 2를 독립 재렌더해 같은 SHA-256 9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f를 얻었다. 생성한 animation_spec.json은 저장소 corrected spec과 동일했고 240개 개별 frames PNG도 모두 바이트 동일하다. 따라서 SHA 9719… MP4와 현 소스의 제작 관계를 재현으로 확인했다.
- Run 1/Run 2에서 같은 9개 캐릭터 소스 레이어 디렉터리를 사용한다. 두 Run의 soundtrack.wav SHA-256도 모두 5a3547051ac00882cb946d14269e7e1a859682e38e6130402b0558d387334d70이다. 정규화한 scene_03_jump 입력 해시는 양쪽 모두 c9edfbc23f2672e042334cd9c3660989d92ee57d799196549d60f0ad2b3c5353이다. 그래서 두 영상의 변경 범위는 제작 기록대로 어깨 피벗(-36→-24px)이며 캐릭터 레이어·음원·점프 장면 입력은 동일하다.
- 수정본 MP4의 바이트 동일성이 이전 10초·12/24fps·hold·오디오 트랙/음량 확인을 보존하므로 이들 전체 검사는 반복하지 않았다. 청감 평가는 여전히 미검증이다.
- 별도 steps=2/waves=2 렌더는 8초/24fps, 192프레임, QA machine flag true다. 이 flag는 기계 항목이며 동작 품질 verdict는 NOT_RATED다. 이 렌더는 경계 입력의 모션 존재만 확인하려는 범위 축소 산출물이다.

### 미평가 표기·보고 잔여 확인

- 현재 render code는 identity 수치 비교를 하지 않고 identity_comparison=not_performed, continuity score=null, rating=not_evaluated, verdict=NOT_RATED를 기록한다. 프레임 간 identity 측정치 1.0, continuity 0.98, 자동 시청 품질 PASS 조건식은 제거됐다.
- 현재 benchmark JSON 작성 코드도 motion_eval 수치를 요약 리포트에 복제하지 않는다. render_manifest의 NOT_RATED는 남는다. 검색한 B 모듈/스크립트/테스트/보정 보고서에서는 이전 고정 1.0/0.98 점수 값이 실행 가능한 품질 주장으로 남아 있지 않았다.
- 자동 Markdown 보고서에는 “미디어 QA 결과” 아래 “최종 판정: True”가 여전히 출력된다. 이것은 video/audio track, resolution, duration, black ratio, volume의 기계검사 합격일 뿐 동작·시청 품질 합격이 아니다. 섹션 안에서는 범위가 제한되지만, 단독 발췌 시 넓은 의미의 최종 판정으로 읽힐 여지는 보고 문구의 잔여 모호성으로 기록한다. 청감/시각 평가를 통과시킨 값은 아니다.
- 변경 관련 테스트 3건은 3 passed in 0.13s. B 소유 코드·스크립트·테스트·프리셋 4개 파일을 실제 uv 가상환경 Python의 py_compile로 확인했고 4개 모두 성공했다. 이전 보고의 shell python not found는 시스템 python 실행 파일 부재였고 uv python으로 좁게 확인했다.

실행 증거:

- 현재 코드 렌더 재현: PYTHONPATH=. UV_CACHE_DIR=/private/tmp/uv-cache-b2 uv run python - <<'PY' (build_benchmark_spec + FrameAnimationRenderer(-24px)로 제 Run outbox 재렌더) → 저장소 수정 MP4 SHA와 동일, spec 동일, 240/240 PNG 바이트 동일.
- 해시 대조 명령: sha256sum outbox/benchmark_b_work/corrected_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4 /Volumes/Backup/ttakkari-data/outbox/887101ac-d201-49f1-9e1a-be9b4c9f0c56/benchmark-b-correction/corrected-source-rerender.mp4 → 두 파일 모두 9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f.
- 모션 회귀: UV_CACHE_DIR=/private/tmp/uv-cache-b2 PYTHONPATH=. uv run pytest unit_tests/test_creative_v2_frame_animation.py::test_walk_cycle_has_measurable_limb_swing_and_bounce unit_tests/test_creative_v2_frame_animation.py::test_wave_cycle_has_multiple_arm_angles unit_tests/test_creative_v2_frame_animation.py::test_risky_root_displacement_is_not_reported_as_viewing_quality_pass -q → 3 passed in 0.13s.
- 컴파일 명령: PYTHONPYCACHEPREFIX=/private/tmp/b-pycache UV_CACHE_DIR=/private/tmp/uv-cache-b2 PYTHONPATH=. uv run python -c "import py_compile; files=['src/ttakkari/creative/v2/frame_animation.py','templates/frame_animation/presets.py','scripts/run_frame_animation_benchmark.py','unit_tests/test_creative_v2_frame_animation.py']; [py_compile.compile(p,doraise=True) for p in files]; print('PY_COMPILE_OK',len(files),files)" → PY_COMPILE_OK 4.
- 값 재현: steps2 leg range −28…28deg, arm range −32…32deg, root_y −14…0px; waves2 arm −20,−140,−115,−140,−165,−140deg. 기본 wave3의 손 흔들기 각도는 −165…−115도(폭 50도), 팔 올리기까지 포함한 전체 범위는 −165…−20도다.

### 이번 Run 공유 산출물

| 산출물 | SHA-256 | share_file ID |
|---|---|---|
| 수정 소스 기본 벤치마크 10초 MP4 | 9719e2388cfd3d1b0467c92fdb00c68c051111244b73b1ffdf270eec9cace00f | 86fe18b1-8b33-445f-87f1-d69780fb51fb |
| steps=2/waves=2 실제 렌더 8초 MP4 | 890c0a13aa135fab6eff84b77526e45dde039a503271409dcc7694eaafbfddb6 | 12351834-c951-46c6-8283-43a8800eb27b |
| 기본 MP4 걷기·인사 선택 프레임 시트 | b13b69f3f8fbab432dced8ed96693efa32503ca9822c71fbd148a6ebda5e6c79 | ed700aa0-e76a-4afb-b8a0-d5b805c5ff40 |
| steps=2/waves=2 MP4 프레임 시트 | f8c0a1d2d53cafb629f1e4043171d3c0345df64632655756b038a697d1761005 | 54a9f928-6fe5-4ad4-8671-67ebc64c00ec |

통합 상태: 이번 motion audit에서 재검사하지 않음. 현재 core가 engines/dispatch를 수정 중이므로 이전 스냅샷의 통합 대기 판정을 현재 결과로 갱신하지 않는다. 모션은 존재하지만 길게 hold되는 표본이 있고, 지각 품질과 청감은 여전히 미평가다.

---

## 후속 독립 검증 — 긴 관절 포즈 보간 및 실제 MP4 (2026-11-07 01:41 KST)

### 범위와 기준점

- 이번 검증은 보간 변경만 대상으로 했다. 전체 2D 벤치마크를 다시 만들지 않았고 producer, presets, benchmark script, core, frontend를 수정하지 않았다. 이전 valid 모션/hash 검증은 다시 계산하지 않고 기존 증거를 참조했다.
- 검사 시작 HEAD와 종료 HEAD는 `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`로 동일했다. 시작 때 직접 기록한 SHA-256: `frame_animation.py` `f2c1ee2216dde8c9be3e2e8e0a10484c56671e139b47cfff6e90f94508f46cb4`, `run_frame_animation_benchmark.py` `223b4038a3153bd9ff369242e45a111f07f4a075ec128d9261e0592be856f0d2`, storyboard `e61823f4b374f4c627f40bfd83c914069129540be7b529d02e8b74c38e1e29b0`. 추가 확인한 변경 관련 preset SHA `48a5423aa4bfad3a15e94a088df5b803f6fe95708803facc72cd8b91356a1fa8`, 테스트 SHA `449c7cc5b7f338b1dbfe61212ef6b0c6948ed405e1cd2e4d285a658ced416f86`는 저장소 색인의 최종 기록과도 일치했다.
- `read_session`에는 작업 카드의 축약 Run 식별자 `0cce8713`만 있어 UUID 검증 오류(422, invalid length 8)로 세션 이벤트를 읽지 못했다. 공유 메타데이터 조회도 새 영상 SHA 검색 결과가 비어 있었다. 이 검증은 세션 진술 대신 지정된 저장소 파일의 원시 바이트·소스·렌더 결과로 판정했다. 다른 Run outbox는 열지 않았다.

```text
$ git rev-parse HEAD
439472b3bc4665edc04ad1d7fa8ad1a9684a47b4
$ sha256sum src/ttakkari/creative/v2/frame_animation.py scripts/run_frame_animation_benchmark.py templates/frame_animation/benchmark_b_storyboard.json
f2c1ee22…  frame_animation.py · 223b4038…  run_frame_animation_benchmark.py · e61823f4…  benchmark_b_storyboard.json
```

### 바이트·스트림 대조

- 저장소의 새 linear MP4 SHA-256은 요청값 `bc1b02234910b9625522b0d4e0c710c57b131288f2da718726a64160ecf94dac`, 같은 입력 hold MP4는 `6ce178c512ef09d60d9d536b029322454ad6ca253b0fa34fc2995fe9c0a817b8`와 각각 일치했다. 기존 보존본 `c4b292c5…`와 `9719e238…`도 기존 전체 SHA와 일치했다.
- linear와 hold의 각 MP4를 실제 FFmpeg `showinfo` 디코드했다. 둘 다 240프레임, 첫 PTS 0, 마지막 PTS 9.958333초다. 컨테이너/각 스트림 길이는 10초, 1280×720이며 video H.264와 audio AAC다. 오디오 스트림은 `ffmpeg -map 0:a:0 -f null -`로 디코드 종료 코드 0을 확인했으나 이는 청취 평가가 아니다.
- 제작 12fps와 재생 24fps를 구분했다. 새 출력의 240프레임/10초는 재생 타임라인이고, production 12fps 키포즈 사이의 관절 수치를 24fps 시각에서 선형 보간한다. 구현은 프레임 영상 추정/Optical Flow가 아니라 keyframe `RiggedPose`의 회전·루트 등 수치 필드를 보간한다.
- 새 보간 run, 같은 입력 hold 참조, 이전 corrected Run 2의 캐릭터 PNG 9개는 이름과 바이트가 모두 같다. soundtrack.wav도 세 파일 모두 `5a3547051ac00882cb946d14269e7e1a859682e38e6130402b0558d387334d70`다. 선형/hold/prior의 jump scene keyframes, 카메라 키프레임, 자막 입력 구조는 동등하다. 따라서 이 비교에서 바뀐 축은 interpolation mode다.

```text
$ sha256sum outbox/benchmark_b_work/pose_interpolation_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4 outbox/benchmark_b_work/pose_interpolation_20261107/run_03_hold_reference/benchmark_b_hold_reference.mp4 outbox/benchmark_b_work/corrected_20261107/run_01_initial/animation_run01_initial.mp4 outbox/benchmark_b_work/corrected_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4
bc1b0223…  6ce178c5…  c4b292c5…  9719e238… (네 전체 SHA와 요청/기존 지문 모두 일치)
$ for f in outbox/benchmark_b_work/pose_interpolation_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4 outbox/benchmark_b_work/pose_interpolation_20261107/run_03_hold_reference/benchmark_b_hold_reference.mp4; do ffmpeg -hide_banner -i "$f" -map 0:v:0 -vf showinfo -f null - 2>&1 | python3 -c 'import re,sys; s=sys.stdin.read(); r=re.findall(r" n: *([0-9]+).*?pts_time:([0-9.]+)",s); print(len(r),r[0] if r else None,r[-1] if r else None)'; done
240 ('0','0') ('239','9.958333') / 240 ('0','0') ('239','9.958333')
```

### 독립 수치 반례와 경계 호환

- 현재 `evaluate_pose`를 직접 호출한 수치 확인: steps=2 걷기의 1.25/1.50/1.75초 왼다리 회전은 linear `21/14/7°`, hold `28/28/28°`다. 인사 waves=2의 4.5/4.6/4.8/5.0초 왼팔은 linear 약 `−136.154/−132.308/−124.615/−116.923°`로 변하고 hold는 `−140°` 고정이다. root_x/root_y도 같은 구간에서 수치 보간되며, 이 값들은 영상에서 관절을 추정한 결과가 아니다.
- 기본 mode 생략과 명시적 `hold`는 모두 이전처럼 직전 포즈를 유지했다. 명시적 `linear`에서 keyframe 직전은 이전 범주형 expression/variant/z_order를 유지하고 정확한 keyframe 시각부터 다음 범주값으로 바뀐다. 2초 경계의 연속 값은 1.999초에서 `39.98°`, 2.000초부터 `40°`; 마지막 4초 키포즈에서는 마지막 값, 이후 시각에서도 마지막 포즈를 유지했다. 빈 목록은 default pose, 단일 키포즈는 시각과 무관하게 같은 pose를 반환했다.
- 범주형 반례 입력은 표정 `smile→wink`, variant `smile→wink`, z_order `1→9`였다. 중간 시각에 범주값을 수치 보간하거나 조기 전환하지 않았고 경계에서 함께 전환했다. 회귀 테스트 9건도 통과했다.

```text
$ PYTHONPATH=.:src UV_CACHE_DIR=/private/tmp/uv-cache-b uv run python - <<'PY'  # evaluate_pose boundary probe
walk 1.25/1.50/1.75s: linear 21/14/7°, hold 28/28/28°; wave local .5/.6/.8/1.0s: linear −136.154/−132.308/−124.615/−116.923°, hold −140°
default == explicit hold; exact keyframe switches categories; last-key/empty/single-key cases retain their contract
```

```text
$ PYTHONPATH=.:src UV_CACHE_DIR=/private/tmp/uv-cache-b uv run pytest unit_tests/test_creative_v2_frame_animation.py -q
.........                                                                [100%]
9 passed in 2.82s
```

### 실제 영상 표본과 품질 판정 범위

- 실제 MP4에서 각 12개 연속 디코드 프레임을 추출해 같은 입력 linear/hold와 기존 `9719e238…` hold를 같은 타임스탬프로 나란히 배열했다. 걷기 표본은 1.000–1.458초, 인사 표본은 4.417–4.875초다. linear 표본에서 사지 포즈 변화가 보이고 hold와 같은 입력의 포즈 고정이 대비된다. 걷기 구간에는 카메라 팬과 캐릭터 전진도 있어 화면 이동을 사지 변화로 오인하지 않도록 구조 수치와 함께 봤다. 인사 구간은 카메라 줌과 팔 변화를 분리해 보았다.
- 5.000초 원본 크기 프레임에서 인사 팔은 들어 올려져 있고, 한국어 자막은 온전히 읽혔다. 검토 표본에서는 잘린 자막, 가짜 문자, 뚜렷한 조인트 틈이 관찰되지 않았다. 이는 선택된 표본 결과다. 10초 전체를 연속 시청하지 않았고, 전체 자연스러움·걷기 템포의 지각 품질은 **미검증**이다. 관절 간극의 픽셀 측정도 수행하지 않았다.
- `BENCHMARK_B_REPORT.md`의 `True`는 비디오/오디오 트랙·길이·화면·검은 화면·음량을 다루는 기계 미디어 QA 문맥으로 한정되어 있고 바로 아래에 시청/청감 미평가가 적혀 있다. manifest/report JSON은 continuity score `null`, rating `not_evaluated`, verdict `NOT_RATED`다. 고정 지각 점수나 자동 품질 `PASS`는 보지 못했다.
- 실제 음원을 들을 수 있는 승인 재생 도구는 현재 도구 목록에서 제공되지 않아 청취는 **미실행**이다. volumedetect나 오디오 스트림 디코드는 청감 근거로 승격하지 않았다.

표본 PNG(검사자: 이 검증 에이전트, FFmpeg 디코드 프레임을 정지 이미지로 확인):

- 현재 Run outbox `b-pose-interpolation-review/walk-linear-hold-previous-mp4-frames.png`
- 현재 Run outbox `b-pose-interpolation-review/wave-linear-hold-previous-mp4-frames.png`
- 현재 Run outbox `b-pose-interpolation-review/linear-frame-120-t5s.png` (원본 1280×720)

### 새로 실행한 검사와 결론

- 변경 관련 frame animation unit tests 9건 통과, `frame_animation.py`, `presets.py`, benchmark script, 관련 unit test의 `py_compile` 성공, 대상 변경 파일 `git diff --check` 및 독립 검증 문서 whitespace 점검에서 오류가 없었다. 전체 프로젝트 회귀는 실행하지 않았다.
- **좁은 보간 교정: 통과(기술 동작 범위).** hold 기본/명시 호환, 수치 보간과 범주형 경계, 마지막 시각 호환, 입력 자산 보존 및 실제 linear MP4의 프레임 존재가 확인됐다. 자연스러운 템포/최종 시청 품질 승인은 별도다.
- **전체 B: 부분.** 긴 정지 포즈 교정의 숫자·표본 움직임과 자동 QA 판정 문구는 근거 범위 안에 있다. 전체 영상 품질·청감은 미검증이고 Studio 통합 및 전체 회귀는 core 통합 담당 범위이므로 여기서 판정하지 않았다. 기존 producer의 전체 품질 미달 판정을 이 좁은 결과만으로 뒤집지 않는다.
- 아직 확인하지 않은 것: 전체 길이의 자연스러움·템포, 실제 청감, 정량 관절틈 변화, Studio end-to-end 통합, 전체 프로젝트 회귀. 브라우저·bpy 실행, 전체 B 재제작, commit/push/deploy는 하지 않았다.

```text
$ PYTHONPATH=.:src UV_CACHE_DIR=/private/tmp/uv-cache-b uv run python -m py_compile src/ttakkari/creative/v2/frame_animation.py templates/frame_animation/presets.py scripts/run_frame_animation_benchmark.py unit_tests/test_creative_v2_frame_animation.py
exit 0
$ git diff --check -- src/ttakkari/creative/v2/frame_animation.py templates/frame_animation/presets.py scripts/run_frame_animation_benchmark.py unit_tests/test_creative_v2_frame_animation.py docs/creative-studio/evidence-frame-animation-20261107.md outbox/benchmark_b_work/EVIDENCE_INDEX.md
exit 0; verification-b 문서 공백/개행 검사: trailing-space 0, final-newline yes
```

시작 → 종료 SHA-256: `src/ttakkari/creative/v2/frame_animation.py` `f2c1ee2216dde8c9be3e2e8e0a10484c56671e139b47cfff6e90f94508f46cb4` → 동일, `templates/frame_animation/presets.py` `48a5423aa4bfad3a15e94a088df5b803f6fe95708803facc72cd8b91356a1fa8` → 동일, `templates/frame_animation/benchmark_b_storyboard.json` `e61823f4b374f4c627f40bfd83c914069129540be7b529d02e8b74c38e1e29b0` → 동일, `scripts/run_frame_animation_benchmark.py` `223b4038a3153bd9ff369242e45a111f07f4a075ec128d9261e0592be856f0d2` → 동일, `unit_tests/test_creative_v2_frame_animation.py` `449c7cc5b7f338b1dbfe61212ef6b0c6948ed405e1cd2e4d285a658ced416f86` → 동일. 이 문서 시작 SHA-256은 `018ca1731873dc32a607f54712d7f17d614f6f12ac3d307218997fbc052481df`였다. 종료 HEAD도 시작과 동일했다. 이 최신 절을 맨 끝에 추가하고 기존 문서 헤더의 후행 공백 두 곳을 제거했다. 관련 구현 변경은 커밋하지 않았다.
