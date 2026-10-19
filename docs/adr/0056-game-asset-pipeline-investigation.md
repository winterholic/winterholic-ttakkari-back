# ADR 0056: 게임 에셋 파이프라인 조사 — 기존 ArtCraft 경로 재사용 설계 (Phase 3, TASK-G22)

- **상태**: 조사와 설계만(구현 없음). 2026-10-19.
- **관련**: ADR 0039(ArtCraft 작업 플랫폼), ADR 0050(worker prompt 의 에셋 규칙), 설계서 §2.6·§8 Phase 3
- **질문**: 게임 worker 가 이미지·오디오 에셋을 만들 때 기존 따까리 도구를 쓸 수 있는가, 새 시스템이 필요한가.

## 조사한 사실 (코드 근거)

1. **이미지·소리 도구는 ArtCraft 앱이다.** 범주 카탈로그(`src/ttakkari/artcraft_catalog.py`)는 PhotoCraft(사진 편집)·VectorCraft(SVG 일러스트)·SoundCraft(오디오 편집) 등을 사용자 언어 범주로 묶고, 각 앱의 되는 일·안 되는 일·성숙도를 이 Mac 에서 직접 돌려 기록한 데이터다. 텍스트로 이미지나 소리를 생성하는 모델 호출 경로는 이 저장소에서 찾지 못했다(검색 범위: `src/ttakkari` 의 image·audio·tts 관련 이름과 문서. 못 찾았다는 것이 없다는 증명은 아니다).
2. **앱 실행은 새 실행기 없이 MCP 로 붙는다.** `artcraft_tasks.prepare_run`(`artcraft_tasks.py:387`)이 Run 직전 작업 폴더를 만들고 앱별 stdio MCP 서버 명세(`mcp_spec`, `artcraft_tasks.py:331`: `bin/artcraft mcp <app>` + `--automation-read-root/--automation-write-root <폴더>`)를 `LaunchSpec.mcp_servers` 에 합친다. 호출 위치는 `runs/manager.py` 의 `artcraft_tasks.prepare_run`(1685행 근처)이고, 실행 후 `sweep_outputs`(1108행 근처)가 결과를 모은다.
3. **경로 제한은 앱마다 다르다.** PhotoCraft·VectorCraft·SoundCraft 등은 `rootflags` 로 읽기·쓰기 루트를 한 폴더에 가둘 수 있고, 일부 앱은 가둘 수 없어 guard 의 부분 보호뿐이다(ADR 0039).
4. **게임 worker 는 이미 에셋 규칙을 받는다.** `games/worker.py` 의 prompt 규칙은 "에셋은 출처와 라이선스를 저장소 안 문서에 함께 기록한다. 출처를 확인할 수 없으면 쓰지 않는다"이고, 생성 API(ADR 0053)의 첫 요청 문장은 외부 에셋 대신 코드로 그리게 한다. 즉 지금 게임은 코드로 그린 에셋만 가진다.
5. **외부 에셋 후보의 라이선스.** Kenney 는 에셋 페이지의 게임 자산을 CC0 로 안내한다(설계서 §11 의 외부 근거). jsfxr npm 패키지는 `npm view jsfxr` 로 버전 1.4.1 과 저장소 URL(github.com/chr15m/jsfxr)은 확인했지만 license 필드는 비어 있어 라이선스는 **확인 필요**다.

## 결정(설계)

새 에셋 시스템·새 표를 만들지 않는다. 구현이 필요하면 아래 경로를 쓴다.

- **만든 에셋 연결**: 게임 worker Run 에 필요한 ArtCraft 앱 MCP 서버를, 게임 저장소의 `assets/` 폴더 하나에만 읽기·쓰기 루트를 가둬 붙인다. `artcraft_tasks.prepare_run` 과 같은 자리에 `games` 쪽 hook 을 하나 두는 방식이며, `rootflags` 를 지원하는 앱(VectorCraft·SoundCraft·PhotoCraft)만 허용 목록에 둔다. 가둘 수 없는 앱은 게임 worker 에 붙이지 않는다.
- **출처 기록**: 저장소 루트의 `ASSETS.md`(에셋 파일, 출처(ArtCraft 앱·버전 또는 외부 URL), 라이선스, 만든 Run id)를 worker 가 쓰게 하고, 빌드 전에 `assets/` 의 파일이 `ASSETS.md` 에 모두 있는지 검사하는 규칙을 후속에서 정한다. 라이선스 항목이 비었거나 확인 필요인 외부 에셋은 빌드를 막지는 않되 관제탑에 경고로 보인다(막을지는 사용자 결정).
- **외부 팩(Kenney)**: 내려받기는 격리 빌드와 같은 이유로 호스트 worker 가 임의 URL 을 받지 않는다. 사용자가 팩을 승인해 `assets/` 에 올리는 경로(Artifact 업로드)를 먼저 쓰고, 자동 검색은 후순위로 둔다.
- **절차적 사운드(jsfxr)**: 라이선스가 확인되기 전에는 의존성에 넣지 않는다. 대안은 worker 가 코드로 Web Audio 합성음을 만드는 것이며 이는 외부 파일이 없어 라이선스 문제가 없다.

## 후속 단위 (각 2시간 이하, PMS 3.4 를 분할)

1. 게임 worker 에 `assets/` 가둔 ArtCraft MCP 연결(허용 앱 목록, 경로 제한 테스트: 폴더 밖 쓰기 거부).
2. ASSETS.md 계약과 빌드 전 검사, 관제탑 경고 표시.
3. 사용자 업로드 에셋 팩을 `assets/` 로 들이는 경로(Artifact → 저장소, sha256 기록).
4. Kenney 팩 선택 연결은 3 이후에 다시 평가한다.

## 하지 않은 것

- ArtCraft 앱을 실제로 게임 에셋 제작에 돌려 보지 않았다(VectorCraft SVG 가 Phaser 텍스처로 쓸 수 있는 품질인지 미검증). 후속 1 의 완료 기준에 실제 실행 한 번을 넣는다.
