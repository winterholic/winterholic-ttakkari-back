# 알림함 구현 — 2026-10-16

상태: 구현·로컬 검증 완료, 운영 미배포. 프론트 커밋 `1d2c78a`와 이 백엔드 변경을 함께 적용해야 한다. 사용자 요청은 알림을 모아 보는 페이지와 유형 탭이며, 배포 요청은 아직 받지 않았다.

## 변경

- `/notifications`: 전체·작업·따까리·일정·할 일·회의록·시스템 탭, 유형별 안 읽은 수, 날짜별 목록, 미확인 필터, 개별/유형별 모두 읽음, 원래 화면 링크, 페이지 추가 조회.
- 모바일 더보기·데스크톱 부가 기능·사용자 메뉴에 알림 진입점. 기존 푸시 설정은 별도 항목과 페이지 헤더에서 유지.
- `notify()`가 푸시 전송 전에 알림함에 저장한다. 구독이 없거나 전송에 실패해도 기록이 남는다. 읽음 상태는 단일 사용자 전체 기기에서 공유. 현재 조회 ID까지 읽음 처리하여 새로 도착한 알림을 보호한다.
- 알림 종류별 명시적 분류: 실패/한도 대기 → 작업, MCP 직접 알림 → 따까리, 일정/체크리스트 → 일정·할 일, 회의 분석 → 회의록, 배포/테스트 → 시스템. 내용 추측으로 분류하지 않는다.
- Alembic `0031_notifications`: 새 테이블만 추가. 과거 감사 로그에는 본문이 없어 과거 알림을 재구성하지 않는다. 운영 적용 이후부터 보관.

## 검증

- 백엔드: 격리 DB `ttakkari_notifications_8c882dc8`에서 `.venv/bin/python -m pytest -q -x` → **840 passed, 2 xfailed, 6 subtests passed in 161.34s**. 알림 전용·기존 Push 회귀는 32 passed. Ruff·diff 검사 통과.
- 프론트: `TTAKKARI_NO_ENV_FILE=1 pnpm test --run` → **452 passed, 1 skipped**. 마지막 터치 영역 보강 후 알림 관련 7개 재검증. `pnpm build`·`pnpm lint` 통과. 기존 큰 번들 경고와 jsdom canvas 안내가 있다.
- 별도 격리 DB `ttakkari_notifications_migration_8c882dc8`에 빈 DB부터 `alembic upgrade head` 성공. 알림 테이블의 migration/model 비교 차이 `[]`. 전체 `alembic check`는 기존 검색·벡터 수동 인덱스 11개를 제거 차이로 보고하므로 전체 통과로 기록하지 않는다. 이번 알림 테이블 차이는 없다.
- 설정된 ttakkari_browser로 샘플 API fixture를 연결해 390×844·1280×900과 라이트/다크 확인. 실제 브라우저에서 탭 전환·키보드 Home·유형별 모두 읽음·미확인 필터 후 빈 상태 확인. 탭 최소 터치 높이 44px. 긴 본문은 줄바꿈. 원래 대화 이동은 컴포넌트 테스트로 확인.
- 브라우저 콘솔의 오류 2개는 Vite 개발 서버에 생성된 `sw.js`가 없어 발생하는 기존 SW MIME 오류. 운영 PWA 수신·실제 사용자 데이터로 검증한 것은 아니다. 프로덕션 빌드는 `sw.js` 생성 성공.
- 화면 시안: Run outbox의 `notifications-mobile-sample.png`, `notifications-desktop-sample.png`, `notifications-dark-sample.png`. 모두 샘플 데이터. 브라우저와 임시 fixture/Vite 프로세스 종료.

## 다음 적용 순서

명시적 배포 요청이 오면 깨끗한 백엔드 커밋으로 `request_backend_deploy`를 예약하고 턴을 종료한다. 실제 서버 적용 및 `/api/notifications` 확인 뒤 프론트를 적용한다. API가 없는 운영 서버에 프론트부터 올리지 않는다. 이번 턴에서는 push·운영 마이그레이션·배포 예약을 하지 않았다.
