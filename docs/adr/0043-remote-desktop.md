# ADR 0043. 아이폰에서 직접 조작하는 원격 데스크톱

2026-10-18. 구현 방식 선정, 실제 Mac·아이폰 사용성 최종 판정은 미검증.

## 목적과 범위

사용자가 따까리에서 자신의 Mac을 열어 브라우저 확인 등 짧은 작업을 끝낸다. AI 조작, 기기 관리, 파일 관리자와 브라우저 관리자는 추가하지 않는다. 기본 화면은 원격 화면이고, 보조 도구는 자동으로 숨긴다. 첨부된 사용자 예시의 핵심은 아이폰 폭에 Mac 전체를 강제 축소하지 않는 것이다.

## 현재 구조와 확인 범위

- 프론트: React 19 / Vite 8 / 기존 ttakkari 토큰·컴포넌트 / PWA. Vercel에 main 자동 배포. 새 `/remote-desktop` 라우트는 지연 로드하고 기존 더보기·부가 기능 목록에 연결한다.
- 백엔드: FastAPI / PostgreSQL. 소유자 비밀번호 + JWT / refresh 쿠키. 공유 링크·에이전트 MCP 인증은 원격 화면 권한으로 받지 않는다.
- 운영 경로: Vercel → 기존 HTTPS API / Cloudflare Tunnel → loopback Caddy → 세대별 FastAPI. `generation_deployment.proxy_config`는 Caddy reverse_proxy와 stream_close_delay를 사용한다. 운영 revision/runtime은 2026-10-18 조사 시 `b427e3175bce7375f5ecb6474840feff7cd41aaa`, `/Users/winterholic/.ttakkari/deploy/releases/<SHA>`였다. 이 경로의 실제 원격 화면 WSS 연결은 미검증이다.
- Codex 명령 환경에서 loopback 5900 TCP 연결이 `ConnectionRefusedError [Errno 61]`였다. `/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json`은 `Failed to load preferences`였다. 호스트에서 화면 공유가 꺼졌거나 Tailscale이 사용 불가능하다는 결론은 내리지 않는다. MacBook의 현재 IP·도달성도 미확인이다.
- 기존 Mac 설정, 접근성·화면 기록 권한, 방화벽, Tailscale ACL을 변경하지 않는다.

## 후보 비교와 결정

아래 비용·적합성은 현재 코드 구조에 대한 설계 판단이다. Mac/iPhone 지연시간·대역폭을 실측한 순위가 아니다.

| 후보 | 통합·유지보수 | 모바일·입력 | 보안·네트워크 | 판단 |
| --- | --- | --- | --- | --- |
| macOS VNC + noVNC | OS 화면 공유와 noVNC RFB/인증/디코더 재사용. FastAPI에 작은 WS↔TCP bridge만 추가 | 화면 배율과 터치 UX를 앱에 맞게 구성. ARD 인증 지원. 한글 서버 수신은 별도 확인 필요 | 기존 HTTPS 소유자 인증 + 재인증 + 일회용 접속권. Mac까지는 loopback/LAN/Tailscale | 1차 구현 선정. 별도 상시 캡처 에이전트 불필요 |
| noVNC + 독립 websockify | 검증된 별도 WS↔TCP 프로세스 | 같은 noVNC 입력 가능 | 별도 인증 게이트·운영 포트·서비스 수명 연결 필요 | 기존 FastAPI에 스트리밍 bridge를 넣는 쪽이 현재 구조에서 작음 |
| Apache Guacamole | 브라우저 클라이언트 + guacd + VNC 플러그인 경로. 다중 프로토콜에 유리 | 기존 터치/키보드 구성 활용 가능 | 별도 세션·인증 통합 필요 | Mac 두 대의 VNC 기능에는 운영 구성 부담이 큼 |
| RustDesk | Mac 클라이언트·서버/릴레이와 웹 배포 옵션 검토 필요 | 전용 클라이언트 및 웹 기능 존재 | 별도 장치·인증·서버 생태계와 연동 | 기존 앱 안에서 짧게 접속하는 범위보다 큼. 가격/라이선스 우열은 판단하지 않음 |
| WebRTC + 네이티브 캡처 | 인코딩·전송·입력 주입·시그널링·권한을 별도로 구성해야 함 | 동영상 압축 경로를 선택할 수 있으나 이번 환경의 지연시간 비교는 미측정 | 브라우저 getDisplayMedia는 화면 선택 UI를 수반. 무인 Mac 접속에는 별도 네이티브 경로 검토 필요 | VNC 실기기 성능이 기준에 못 미칠 때 재평가. 지금 자체 원격 시스템을 크게 만들지 않음 |

공식 근거:

- [Apple 화면 공유와 VNC 설정](https://support.apple.com/ko-kr/guide/mac-help/mh11848/mac)
- [noVNC API](https://github.com/novnc/noVNC/blob/v1.7.0/docs/API.md), [지원 인증·프로젝트](https://github.com/novnc/noVNC). npm 1.7.0 고정. 라이브러리 소스는 수정하지 않고 프론트 `public/licenses/novnc/`에 배포 고지를 둔다.
- [Guacamole 아키텍처](https://guacamole.apache.org/doc/gug/guacamole-architecture.html)
- [RustDesk 웹 클라이언트 배포 옵션](https://www.rustdesk.com/blog/rustdesk-web-client-v2-preview/)
- [MDN 화면 캡처 API](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Capture_API/Using_Screen_Capture)

## 권한·통신

1. 등록 목록·변경·접속권 발급은 기존 owner JWT가 필요하다.
2. 기기 등록과 최초 제어 허가는 따까리 비밀번호를 재확인한다. 로그인 실패 제한을 함께 사용한다. 기기별 제어 허가는 메모리에서 10분 유지한다.
3. 30초짜리 일회용 티켓은 소유자 Origin과 기기에 묶는다. WebSocket 첫 프레임으로 티켓·허가를 전달하며 URL에 넣지 않는다. 잘못된 Origin·티켓은 Mac에 TCP 연결하기 전에 거부한다.
4. 서버가 승인한 기기만 연결한다. 임의 URL·DNS 이름을 받지 않는다. loopback, RFC1918 LAN, Tailscale IPv4/IPv6와 5900–5999 포트만 허용한다. 변경은 PostgreSQL advisory transaction lock으로 직렬화하고 최대 8대를 둔다. 초기 목록은 loopback Mac Studio 한 대다.
5. 동시에 최대 2개 bridge/세대, 미인증 handshake 5초, TCP 연결 4초, 스트림 2시간 상한, 클라이언트 바이너리 프레임 1MiB 상한. 두 방향 중 하나가 끝나면 작업과 TCP를 정리한다. 연결 해제는 허가·대기 티켓을 폐기하고 열린 스트림도 중단한다.
6. Mac 로그인/VNC 비밀번호는 브라우저 메모리에서 noVNC에만 전달한다. DB·localStorage·URL·감사 로그에 저장하지 않는다. 서버에 저장하는 것은 기기 주소·이름과 허가 감사 기록이다.
7. Mac Studio는 loopback. MacBook 등은 서버에서 닿는 LAN/Tailscale IP를 등록한다. 아이폰은 기존 HTTPS 앱/API를 사용할 수 있으므로 폰의 Tailscale 연결만으로 허가하지 않는다. Mac까지의 외부 경로에는 Tailscale을 사용한다. 새 인터넷 수신 포트나 방화벽 규칙은 만들지 않는다.
8. 배포 세대 변경·프로세스 재시작은 메모리 허가를 폐기한다. 다시 인증해야 할 수 있다. 화면 접속 때문에 기존 세대의 AI Run drain 정책을 바꾸지 않는다.

## 모바일 UX

- 기본은 원래 픽셀 배율에서 잘라 보고 이동한다. 전체 맞춤은 명시적으로 누른다. Mac 해상도는 바꾸지 않는다.
- 기본 트랙패드: 한 손가락 이동, 탭·더블탭 클릭, 길게 눌러 드래그, 두 손가락 스크롤, 핀치 확대. 확대 시 커서를 따라 뷰포트가 이동한다.
- 직접 터치: 탭한 곳 클릭, 한 손가락으로 화면 이동, 길게 눌러 드래그. 별도 화면 이동 모드와 우클릭 버튼도 제공한다. 일반 마우스는 절대 좌표·버튼 상태를 전달한다.
- 도구는 5초 뒤 숨긴다. 키보드 도구에서는 일반 도구를 접고 특수 키도 필요할 때 펼친다. safe area와 visualViewport 높이를 적용한다.
- iOS 한글은 로컬 textarea에서 조합한 뒤 한 번에 Unicode 키 이벤트로 보낸다. 조합 중간 값은 보내지 않는다. 실제 Mac VNC 서버가 이 입력을 받아 문자를 만드는지는 실기기 검증 항목이다. 기본 VNC 클립보드의 Latin-1 대체 문자 손상을 피하려고 한글의 해당 전송 경로는 막는다.
- Cmd/Option/Control/Shift는 다음 키에만 적용하고 즉시 해제한다. 포커스 상실·제스처 취소·연결 종료에서 누른 마우스 버튼을 해제한다.
- 네트워크 단절 시 1/2/4초 간격으로 최대 세 번 재접속하고 실패 시 다시 연결·계정 확인을 제공한다. Mac 화면 공유 미응답은 설정·잠자기·경로 확인을 안내한다. 깨우기·FileVault 부팅 전 로그인·전원 켜기를 보장하지 않는다.
- 파일 전송·원격 해상도·품질 조절 메뉴는 추가하지 않았다. 기존 따까리 파일 전송을 별도로 사용할 수 있다.

## 검증과 남은 완료 조건

테스트 코드: `unit_tests/test_remote_desktop*.py`, 프론트 `interaction.test.ts`, `RemoteDesktopPage.test.tsx`. 전용 RFB fixture는 실제 앱 입력→WebSocket→FastAPI bridge→TCP 서버 수신까지 확인하며 운영 DB나 Mac 화면을 쓰지 않는다. `unit_tests/remote_desktop_fixture.py`와 프론트 `scripts/remote-desktop/`는 검증 전용이며 배포된 진입점이 아니다.

운영에서 usable 판정을 내리기 전 실제 Mac Studio/MacBook 화면 접속, Safari/PWA 키보드·한글 조합/수신, 외부 LTE/5G·Tailscale 경로, 화면 잠금·잠자기, 드래그·스크롤 조작감, 가로 회전·키보드 높이, 30분 이상 접속을 확인해야 한다. Chromium에서 가상 화면을 확인한 결과를 위 실기기 결과로 대신하지 않는다.
