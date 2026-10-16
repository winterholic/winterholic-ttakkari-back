# 서버 리소스 GPU 모니터링 — 2026-10-16

사용자가 서버 리소스 화면에 GPU도 모니터링하도록 기능 추가를 요청했다.

- Mac Studio의 IOAccelerator 텍스트 출력에서 Apple M5 Max, gpu-core-count=40, Device Utilization %=3을 실제 관측했다. system_profiler의 GPU 40코어와도 일치했다. XML(-a) 출력은 이 환경에서 `ioreg: error: can't open file.`이었지만 텍스트 수집은 성공했다. 운영 GPU 접근 불가라는 뜻이 아니다.
- sysinfo에 GPU 모델·코어 수·전체 사용률·사용/할당 시스템 메모리를 추가했다. 관리자 권한·외부 패키지 없이 고정 ioreg 인자, timeout=2s, 기존 2초 캐시를 사용한다. 읽기 실패·잘못된 값은 null과 상태/사유로 남긴다. 유휴 0%와 구분하고 CPU 등 수집은 유지한다. 코어별 GPU 사용률·전용 VRAM·전력/온도는 제공하지 않는다.
- 프론트에 기존 게이지/디자인 토큰으로 GPU 카드를 추가하고 기존 코어별 영역을 CPU 코어별로 명시했다. API에 gpu가 없는 이전 서버와도 호환된다.
- 적용 전문가: dev-python(표준 라이브러리 수집·실패 경계), TDD, frontend-design(기존 위계·상태 재사용), webapp-testing. 별도 작업자 위임은 하지 않았다.
- TDD: unit_tests/test_gpu_resources.py 최초 15 failed(_gpu 부재), 구현 후 15 passed. 프론트 최초 5 failed/3 passed(GPU 표시 부재), 구현 후 8 passed.
- 검증: 격리 DB ttakkari_gpu_test_20261016에서 GPU 단위+리소스 API 검사 20 passed. 변경 파일 ruff, 프론트 tsc/build/변경 파일 ESLint 통과. Vite 기존 500KB 청크 경고는 남는다.
- frontend-design 정적 검사 ERROR 0/WARN 1. 경고는 기존 CPU 코어 배열의 index key이며 코어 번호와 고정 대응해 유지했다. 격리 테스트 DB와 임시 프리뷰 파일/서버/브라우저는 검사 후 정리했다.
- 실제 _collect() 스냅샷에 gpu.status=available, cores=40, percent=0.0, memory_used_bytes=507625472를 확인했다. 이 값은 명령 실행 샌드박스에서 얻었으며 운영 인증 API 응답을 실행한 증거로 대체하지 않는다.
- 지정 ttakkari_browser로 실제 ResourcesPage/ResourcesPanel에 이 스냅샷을 공급해 390×844, 320×740, 1280×900에서 GPU 카드·CPU 구분을 확인했다. 수집 실패/이전 서버 응답은 —와 안내를 표시하고 GPU 막대가 없다. 페이지 제목 갱신 카운터 1→5로 폴링 확인. 콘솔 오류는 검증 페이지 favicon 404 한 건이었다. 운영 로그인·실기기 PWA는 미검증.
- 브라우저 증거 및 실측 gpu-sample.json: outbox/fa5476ae-6b62-40f7-b5c9-087f0d4fcf8a/.
- 독립 worktree: /private/tmp/ttakkari-gpu-back, /private/tmp/ttakkari-gpu-front. 원본 front의 AccountsSection 관련 미커밋 변경은 건드리지 않았다. 최종 배포 예약 결과는 기획 세션 보고를 따른다.
