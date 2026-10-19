# ADR 0044: AI 게임 개발 스튜디오 1차 지원 엔진 어댑터 선정 (Web - Phaser 3)

- **상태**: 채택 (2026-10-19). Phase 2 최소 기능 구현 및 E2E 검증용 첫 어댑터 확정.
- **성격**: 영구 표준의 성급한 고정이 아닌, 'Phase 2의 화면 중심 관제탑 및 피드백 루프 실측을 위한 1차 검증용 어댑터'로 기술함.
- **실행 근거**: 따까리 AI 게임 개발 스튜디오 기획 및 설계서 (`back/docs/design/game-studio.md`).

---

## 1. 맥락과 문제 정의

따까리 AI 게임 개발 스튜디오의 목표는 코드와 엔진을 모르는 사용자가 모바일 폰에서 '게임 디렉터'로서 아이디어만 제공하면, AI 에이전트들이 개발, 빌드, 플레이 테스트, 시각 피드백 수정 루프를 자율 완결하는 것이다.
이를 실현하기 위해서는 관제탑에서 사용자가 지연 없이 게임을 직접 플레이할 수 있어야 하고, 화면의 스크린샷 캡처 및 특정 게임 오브젝트(엔티티)를 선택해 AI에게 수정을 요구할 수 있어야 한다.

이 단계에서 첫 번째로 지원할 게임 엔진 어댑터를 선정해야 한다.
후보 기술 스택은 다음과 같다:
1. **Phaser 3 (Web 2D 풀 프레임워크)**
2. **PixiJS v8 (Web 2D 고속 렌더러)**
3. **Three.js / React Three Fiber (Web 3D)**
4. **Godot 4 (오픈소스 범용 엔진, 헤드리스 & Web Export)**
5. **Unity / Unreal Engine (상용 대형 엔진)**

---

## 2. 후보 비교 및 실측 평가

| 평가 기준 | Phaser 3 | PixiJS v8 | Three.js | Godot 4 | Unity / Unreal |
|---|---|---|---|---|---|
| **로컬 환경 실측 (Mac Studio)** | **즉시 가능** (Node 26, npm 11) | **즉시 가능** (Node 26, npm 11) | **즉시 가능** (Node 26, npm 11) | **미설치** (`godot not found`) | **미설치** (`Unity/Unreal not found`) |
| **AI 코드 생성 신뢰도** | **최상** (방대한 2D 예제, 표준 패턴) | **보통** (물리/사운드 라이브러리 결합 결함) | **보통~낮음** (3D 좌표/물리/수학 환각) | **보통** (GDScript/C# 버전 파편화) | **보통** (방대하고 복잡한 프로젝트 구조) |
| **관제탑 임베딩** | **iframe 정적 서빙 (0ms 지연)** | **iframe 정적 서빙 (0ms 지연)** | **iframe 정적 서빙 (0ms 지연)** | WASM Web export or VNC | WASM Web export or VNC |
| **모바일 터치 조작** | **우수** (PointerEvent 즉시 매핑) | **우수** | **보통** (카메라 조작 난이도) | 보통 (WASM 캔버스) | 복잡 (가상 컨트롤러 필요) |
| **오브젝트 선택 (Picking)** | **용이** (Scene Graph postMessage) | 가능 (DisplayObject 트리) | 가능 (Raycasting) | 디버그 소켓 연동 필요 | 원격 RPC 필요 |
| **빌드 속도** | **1~3초** (Vite / esbuild) | **1~3초** (Vite) | **1~5초** (Vite) | 15~30초 (헤드리스 컴파일) | 수 분 ~ 수십 분 |
| **무인 플레이 테스트** | **용이** (Playwright 헤드리스) | **용이** (Playwright) | **용이** (Playwright) | GUT 엔진 러너 필요 | 엔진 내부 테스트 프레임워크 |
| **라이선스** | MIT | MIT | MIT | MIT | 독점 라이선스 / 계정 필요 |

---

## 3. 결정

**Phase 2 검증용 첫 엔진 어댑터로 `Web - Phaser 3 (HTML5/TypeScript/Vite)`를 선정한다.**

### 선정 근거:
1. **로컬 환경 실측 완비**: Mac Studio에 Node.js `v26.9.0`, npm `11.19.1`이 이미 설치되어 있어 추가 소프트웨어 설치 없이 즉시 실행 가능하다.
2. **AI 에이전트 자율 생성 성공률 극대화**: Phaser 3는 Arcade Physics, 스프라이트 애니메이션, 씬 관리, 사운드, 타일맵이 모두 내장된 'Batteries-Included' 프레임워크다. PixiJS처럼 외부 물리 엔진(Matter.js)을 조합하거나 Three.js처럼 복잡한 3D 벡터 수학을 AI가 자율 코딩할 필요가 없어 결함 발생률이 가장 낮다.
3. **관제탑 PWA(아이폰) 최적화**: iframe 정적 서빙을 통해 0ms 지연시간으로 폰에서 직접 플레이할 수 있으며, 캔버스 스크린샷 캡처 및 `postMessage`를 통한 씬 오브젝트 픽킹(Entity Picking) 구현이 가장 단순하다.
4. **무인 회귀 테스트 자동화**: 별도의 엔진 GUI 없이 Playwright 헤드리스 브라우저를 통해 키보드 입력 주입, 콘솔 오류 수집, FPS 측정이 100% 자동화된다.

---

## 4. 감수한 트레이드오프 및 한계

1. **3D 게임의 미지원**: Phaser 3는 2D 전용 프레임워크이므로 3D 그래픽 게임은 제작할 수 없다. (이는 Phase 3~4에서 Three.js 및 Godot 어댑터를 확장하여 해소한다.)
2. **복잡한 물리 시뮬레이션의 한계**: 고사양 물리나 대규모 파티클 연산은 네이티브 C++/Rust 엔진에 비해 브라우저 JS 런타임 제약을 받는다. 1차 단계에서는 캐주얼 2D 아케이드, 도트 러너, 슈팅, 플랫포머 장르에 집중한다.
3. **브라우저 보안 격리 필요**: iframe 실행 시 게임 스크립트가 부모 따까리 PWA의 인증 토큰이나 쿠키에 접근하지 못하도록 전용 CSP 및 iframe sandbox(`allow-scripts allow-same-origin allow-pointer-lock`) 적용이 강제된다.

---

## 5. 향후 확장 및 대체 계획 (Phase 3~4)

- **Godot 4 어댑터**: 2차 타깃으로 지원. Godot 4의 헤드리스 웹 export(`--export-release "Web"`) 파이프라인을 구축하여 고품질 2D/3D 크로스플랫폼 게임으로 확장한다. (전제: Mac Studio에 Godot 바이너리 및 Web Export Templates 설치).
- **Three.js / React Three Fiber 어댑터**: 3D 웹 게임 전용 어댑터로 3차 타깃 확장.
- 모든 확장은 `GameEngineAdapter` 추상 인터페이스를 준수하므로 관제탑 UX 및 피드백 파이프라인은 수정 없이 그대로 재사용된다.
