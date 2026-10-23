# 2026-10-23 자동 소재 생성 TTS 단계 결함(Settings 이름, 이벤트 루프)과 경로 감사

lease 수정(a2a0ad9) 배포 뒤 계획 5809bb7f 재시도에서 s1~s3 Comfy 이미지는 성공했고(LeaseBusy 없음), 다음 단계인 Supertonic TTS에서 새 결함으로 실패했다.

## 증상
생성 c46e0b0d-8c21-4ee6-aba9-00f83577b52c, 2026-10-23T07:59:13Z: `AttributeError: 'Settings' object has no attribute 'local_tts_max_chars'`. receipt null, 자산 0/3, ACE-Step 미시작.

## 결함 1: 존재하지 않는 Settings 필드 (원인 확정)
- 참조를 넣은 커밋: `0177e6a feat(creative): generate v2 production assets locally`(`asset_generation_managed._synthesize_voice`). `git log -S local_tts_max_chars -- src`는 이 커밋 하나다.
- `config.py`의 TTS 한도 필드는 `local_tts_max_text_chars`(기본 5000)와 `local_tts_speak_max_chars`(500)뿐이고 `local_tts_max_chars`는 어느 브랜치에도 없었다. 미병합 feature 설정이 아니라 처음부터 틀린 이름이다.
- 기존 테스트 `test_creative_v2_asset_generation.py`는 generator 전체를 FakeGenerator로 바꾸고 `blockers()`만 직접 호출해 `_synthesize_voice`의 Settings 참조를 한 번도 실행하지 않았다.
- 수정: `local_tts_max_text_chars`. 오류 문구에 한도 값을 넣었다.

## 결함 2: TTS 실행 예산이 다른 이벤트 루프에서 돈다 (감사에서 발견, 실험으로 재현)
- `generate()`는 `asyncio.to_thread` 워커 스레드에서 돈다. `_synthesize_voice`는 그 안에서 `asyncio.run()`으로 새 루프를 만들고 `service.budget()`을 쓴다.
- 운영 세대 모드에서는 `generation.current`가 설정돼(`generation_service.py:296`) `budget()`이 `generation.work("tts")`를 타고, 이것이 서버 루프에 묶인 asyncpg 풀(`refresh()`의 `sessionmaker()`)을 쓴다. 단위 테스트는 `generation.current is None`이라 이 경로를 타지 않았다.
- 재현(수정 전 코드): 서버 루프에서 풀 연결을 만든 뒤 `asyncio.to_thread(_synthesize_voice)` 호출 → `RuntimeError: Task ... got Future ... attached to a different loop`.
- 수정: `ManagedAssetGenerator(loop=...)`. 서버 루프가 실행 중이면 `asyncio.run_coroutine_threadsafe(...)`로 그 루프에서 실행하고, 없으면(테스트·CLI) 기존처럼 `asyncio.run`. `api/creative_v2._run_asset_generation`이 `asyncio.get_running_loop()`를 넘긴다.
- 회귀 테스트는 DB 없이 같은 조건(서버 루프에서만 유효한 자원)을 흉내 낸다. 로컬 DB가 없는 pre-push 훅에서도 도는 테스트여야 해서 실제 DB 풀 재현은 위 일회성 실험으로만 했다.
- 이 결함은 운영에서 아직 드러나지 않았다(결함 1이 먼저 막았다). 위 실험과 코드 경로 근거로 확정한 잠재 결함이다.

## 경로 감사 결과 (같은 유형: 누락 설정·속성, mock, 경로·모델 참조)
- 실제 `Settings()`, 실제 `tts.service`(make_plan·budget·청크 분할), 실제 `cache.concat_wavs`, 실제 lease manager, 실제 ffmpeg mix·inspect_media, 실제 `put_asset`·`generate_assets`·dispatch·렌더까지 통과하는 테스트를 추가했다. 대체한 것은 Comfy 작업(즉시 성공 receipt, 실제 mp4), Supertonic 워커 호출, ACE-Step 자식 프로세스(스텁 스크립트)뿐이다. 위 두 결함 외에 이 경로에서 추가로 깨진 곳은 없었다.
- ACE-Step 작업 JSON 키는 `run_acestep_job.py`가 읽는 키와 맞고(`out, task_type, caption, lyrics, language, duration, seed, bpm, thinking, use_lm, dit, steps, format`), 결과는 `metrics.json`의 `audio_path`로 읽는다. 실제 ACE-Step 실행은 GPU(MPS)가 필요해 샌드박스에서 실행하지 못했다: 미실행.
- 음성 길이 검사(`voice_seconds <= scene_seconds + 0.2`)를 계획 5809bb7f의 실제 대본으로 점검했다. 샌드박스 CPU에서 실제 Supertonic으로 합성: s1 6.27s/7.60s, s2 5.64s/7.47s, s3 7.00s/8.73s, 전부 한도 이내.
- 호스트 preflight: 실패한 생성의 시작 시각(07:58:22)에 `blockers()`가 통과해 배포된 백엔드 호스트에서 Supertonic availability ready, ACE-Step 실행 파일·체크포인트 존재, Comfy 모델·장치 점검이 통과한 것을 확인했다. 샌드박스에서도 같은 경로를 읽기 전용으로 점검했고 일치한다(`availability=ready`, 3개 ACE-Step 파일 존재). 실제 합성 능력은 별개로 위 측정으로 확인했다.

## 테스트
`unit_tests/test_creative_v2_asset_generation_e2e.py` 4건. 수정 전 코드에서 결함 1은 2건 실패(운영과 같은 AttributeError), 결함 2는 1건 실패(different loop), 수정 후 4건 통과.
