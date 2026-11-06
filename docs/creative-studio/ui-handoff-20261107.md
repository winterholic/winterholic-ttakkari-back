# Creative production UI — 진행 인계

## 현재 상태

원래 목표는 자유 의도에서 계획·스토리보드·여러 제작 엔진·검수·부분 수정까지 이어지는 AI 제작 감독 경험이다. UI 작업은 부분이며 실제 제작 화면과 실제 제작 여정은 미검증이다. 메인 목표와 요구 판정 정본은 기획 세션 manage_work이며 이 파일은 UI 재개에 필요한 부분만 기록한다.

- UI 소유자: `9f82d70e-5836-4e2b-9c94-f35f77f7b2d7`.
- frontend: `/Users/winterholic/development/projects/winterholic-ttakkari/front-creative-production-20261107`.
- backend: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`.
- 현재 core: `d93c9153-076c-4479-9a65-3b61de244221`; 이전 core `86c93b9b-...`는 취소되어 재호출하지 않는다.
- 공개 계약: `docs/creative-studio/engine-contract-20261107.md`; 원래 요구는 `docs/design/creative-studio-v2-requirements.md`와 기획 manage_work 원문.
- 이전 전체 작업 맥락: `/Users/winterholic/.claude/handoffs/tasks/creative-studio-v2.md`. 이 파일의 10월 경로·숫자·제약은 역사이며 현재 실행 상태로 승격하지 않는다.

## 다음 할 일

1. 원래 필수 UI 요구와 현재 구현을 대조하여 미지원 표시로 남긴 첨부·품질 목표·장면 미리보기·변경 비교/복원 및 자연어 수정의 실제 공백을 core와 연결한다. 백엔드 미지원 표시는 요구 완료가 아니다.
2. 스토리보드·런타임/엔진 선택 근거·장면/샷 상태·자산·QA unknown/fail·부분 재제작의 실제 API 계약을 연결한다. 기존 프로젝트와 상세 URL을 보존한다.
3. 실제 데스크톱·모바일 제작 화면을 승인된 `ttakkari_browser`로 검수한다. 로그인 화면 캡처를 콘텐츠 화면 검증으로 세지 않는다. 유효한 테스트 로그인·격리 backend를 기존 테스트 설정에서 찾고 인증/인가를 유지한다.
4. 사용자가 이미 승인한 테스트 A brief로 실제 계획→장면→렌더·수정 여정을 수행한다. brief 미지정을 이유로 중단하지 않는다. 테스트 brief: Kafka 프로듀서→브로커→컨슈머의 메시지 흐름을 정확한 한영 텍스트로 설명하는 16초 16:9 영상. B/C 단위 제작은 별도 소유자 작업을 재사용한다.
5. 실제 backend 인증이 막히면 그 표면만 미검증으로 남기고, 명시된 API fixture를 쓰는 UI 검수와 실제 API 검증을 구분해 나머지 구현을 계속한다. fixture 화면을 실제 영속 제작 성공으로 표시하지 않는다.

## 검증 근거와 한계

- frontend HEAD는 확인 당시 `31de7817313636331f9e414cb5400a2afa5baeb4`이며 새 UI 변경은 미커밋이다.
- UI Run `257a80aa-4399-4a99-a12d-02233088fd52` events seq197: `pnpm exec vitest --run src/pages/creative/CreativePlanPage.test.tsx src/pages/CreativePage.test.tsx` → 2 files / 27 passed. seq198: `pnpm build` → built in 366ms; 번들 500kB 경고. 이 기록은 코드 검사이며 실제 UI 품질 증거가 아니다. 변경 없는 범위를 무의미하게 반복하지 않는다.
- `http://localhost:5174/creative`는 격리 브라우저에 로그인이 없어 로그인 화면으로 이동했다. `/api/auth/refresh`, `/api/health`는 `127.0.0.1:8787`에서 CORS 오류였다. 브라우저 launch 자체가 실패했다는 기록은 아니다. 인증·same-origin 개발 proxy 등 정상 경로를 조사하되 보안 설정을 우회하지 않는다.
- 전 UI 보고는 원래 Run outbox의 review.md와 로그인 캡처를 가리켰다. 다른 Run 원본을 열지 말고 이 문서·저장소·지원 공유 메타데이터로 이어간다.
- 신규 제작·청감·실제 화면 비교·복원은 아직 실행하지 않았다.

## WIP 파일과 소유권

- `src/App.tsx`: `/creative` 통합 진입점 및 `/creative/legacy` 기존 편집기 라우팅.
- `src/api/creativeV2.ts`: 서버 제공 필드의 클라이언트/타입.
- `src/pages/creative/CreativePlanPage.tsx`: 자유 설명·선택 입력·프로젝트/계획 목록·장면 근거·QA/최종 미리보기·실패 재시도/변경 구간 렌더.
- `src/pages/creative/CreativePlanPage.test.tsx`: 관련 회귀.
- `src/pages/creative/creativePlan.css`: 전용 스타일.
- 원래와 대기 지시 원문: `docs/creative-studio/ui-pending-instructions-20261107.json`.
- backend 소유 파일·다른 연구 UI 변경·원래 작업 결과를 덮어쓰지 않는다. 본 단계 commit/push/배포는 총괄 통합 단계에서 수행한다.

## 발견한 경계

- 형제 worker에 `message_worker`는 404가 발생한다. 계약을 저장소에 기록하고 기획 세션에 보고해 전달한다.
- 품질 `prepare_review`와 `plan_partial_repair`는 각각 자료 준비와 순수 수정 계획이다. 실제 독립 평가/재제작과 분리한다.
- B는 실제 걷기·wave 동작 결함 교정 중이다. C는 어두운 영상·안전 계약 교정 중이고 Codex bpy 신규 렌더가 SIGSEGV로 막혔다. 파일 존재·인코딩 성공을 품질 PASS로 표시하지 않는다.
- 현재 Run에서 허용된 outbox만 사용하고 공유할 산출물은 share_file 한다. 새 유료 결제·자격증명 파일 읽기 금지.

## suggested skills

frontend-design, ui-refine-loop, webapp-testing을 해당 범위에 적용한다. 실제 동작 변경은 의미 있는 반례로 검증하고 고정 UI 배치만 반복 검사하지 않는다. 독립 감사 세션 `4b2802ba-c13f-4068-a2bf-18faacfbc229`는 구현에 참여하지 않으며 최종 인계는 총괄이 수행한다.
