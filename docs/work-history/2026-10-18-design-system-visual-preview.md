# 디자인시스템 시각 미리보기

사용자의 모바일 UX/UI 및 preview.html 렌더링 요청에 따라 ADR 0034를 확장했다. owner API의 별도 preview 응답으로 같은 커밋의 HTML·CSS·JS·이미지를 self-contained 문서로 묶는다. 기존 MCP 3종과 agent 권한/출력 한도는 유지하며 토큰 요약에 테마별 값을 추가했다.

검증: 원본 고정·미커밋 제외·예제 한정·CSS/이미지/스크립트 번들·경로/인코딩 traversal·symlink·CSS 순환·크기 한도·테마 초기화·HTML title 검사와 owner JWT/agent 세션 분리, 기존 MCP 카탈로그 계약을 검사했다. 격리 DB `ttakkari_ds_mobile_20261018_01` 사용. 실제 명령/결과는 Run outbox `474a19c0-1b82-4ece-9be4-0e5ca2ac9131` 로그에 남겼다. 브라우저는 실제 Git 원본과 루프백 테스트 API를 사용했고 운영 로그인·실제 iOS는 미검증이다.
