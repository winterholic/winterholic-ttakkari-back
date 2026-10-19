# ADR 0055: 두 번째 엔진 어댑터 후보 — Three.js (web-three) 우선, Godot 보류 (Phase 3, TASK-G23)

- **상태**: 결정(구현은 후속 단위). 2026-10-19 이 Mac 에서 실측했다.
- **관련**: ADR 0046(첫 어댑터 Phaser), 설계서 §3.2·§8 Phase 4
- **범위**: 후보를 비교해 다음 엔진을 정한다. 어댑터 구현·템플릿은 이 ADR 에 없다.

## 실측 (2026-10-19, 이 Mac, 호스트에서 직접 실행한 명령)

| 항목 | 명령 | 결과 |
|---|---|---|
| Godot 설치 여부 | command -v godot godot4 Godot; ls /Applications 에서 godot·unity·blender 검색 | 실행 파일·앱 없음 |
| Godot 설치 후보 | brew info --cask godot | Godot Engine 4.7.2 cask 있음(설치하지 않음) |
| Three.js 라이선스·크기 | npm view three version license dist.unpackedSize | 0.186.1, MIT, 압축 해제 약 20.4MB |
| Phaser(비교) | npm view phaser@3.90.0 license dist.unpackedSize | MIT, 약 146.9MB |
| Three.js 격리 빌드 | 현재 IsolatedBuildRunner.build_project 로 three + vite + typescript 프로젝트 빌드(어댑터 변경 없음) | 성공. 설치 4.6초(egress 허용은 registry 만), 빌드 2.0초(네트워크 없음), 총 6.8초. 산출물 index.html 539B + JS 1개 514,753B |
| Three.js 실행 | 빌드 결과를 로컬 http 서버로 열어 헤드리스 Chromium 390x844 에서 확인 | canvas 생성, WebGL2/WebGL 컨텍스트 획득, 배경색이 아닌 픽셀 725/4096, 콘솔 에러는 favicon 404 하나뿐 |

증거 스크립트는 작업 중 임시 경로에 있었고 위 표의 수치가 기록이다. 보존이 필요하면 후속 단위에서 tests 로 옮긴다.

## 결정

1. **다음 엔진은 Three.js(`web-three`)로 한다.** 이유는 모두 실측이다. 현재 격리 러너와 npm·vite 경로에서 코드 변경 없이 빌드되고(6.8초), 별도 게임 origin 의 sandbox iframe 에서 쓰는 산출물 형태(정적 index.html + JS)가 Phaser 와 같으며, 라이선스가 MIT 이고, 이 Mac 의 헤드리스 Chromium 에서 WebGL 렌더가 확인됐다.
2. **Godot 은 이번에 구현하지 않는다.** 이 Mac 에 Godot 이 설치돼 있지 않고(위 실측), 웹 export 에는 export 템플릿 내려받기·헤드리스 export 명령·sandbox iframe 에서의 SharedArrayBuffer/COOP·COEP 요구 여부 같은 미확인 항목이 남는다. 이 항목들은 설치 후 실측이 필요하므로 확인 필요로 두고, 설치 자체는 시스템 변경이라 사용자 결정 뒤에 한다.
3. Three.js 후보는 3D 입력(터치 카메라)·모바일 WebGL 성능·iOS Safari 가 미검증이다. 헤드리스 Chromium 의 소프트웨어 렌더 결과는 실기기 성능을 보장하지 않는다. 템플릿 단위에서 bridge(capture·pause·state)가 WebGL 에서 빈 이미지가 되지 않도록 `preserveDrawingBuffer` 를 켜는 것까지 확인한다(probe 템플릿에는 켜 두었고 캡처 왕복은 아직 실측하지 않았다).

## 후속 단위 (각 2시간 이하, PMS 4.2 를 분할)

- web-three 템플릿과 어댑터: Phaser 어댑터와 같은 계약(`scaffold_project`·`build`), bridge.ts 재사용 방식(공통 파일 복사 vs 엔진별 파일)을 정한다. 완료 기준은 scaffold→격리 빌드→별도 origin iframe 에서 canvas 와 캡처 왕복.
- 생성 API 의 `engine` 허용 목록과 관제탑 폼 선택지 확장(ADR 0053 은 `web-phaser` 만 받는다).
- 엔진별 기능 매트릭스(설계서 §3.2)에 Three.js 행을 실측값으로 채운다.
- Godot 은 사용자가 설치를 승인하면 별도 실측 단위로 다시 연다.
