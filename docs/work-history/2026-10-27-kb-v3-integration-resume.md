# KB v3 재개: 기존 병합 정리와 최신 main 차이

대상은 back-kb-v3-final-5787f6de, 입력 77c1238a + 1b91fa4bc46be8227187ea30bf860f631450b82f. 기존 staged 0074 merge revision과 generation contract를 유지했다.

이번 Run 소유 테스트 DB marker의 Run ID/port/role을 검증하고 실제 PostgreSQL로 실행했다. 계약 초기 검사 3 failed/49 passed는 모두 기존 0073 HEAD 고정 기대였다. 새 통합 0074와 양쪽 0073 계보·online 목록을 검사하도록 갱신한 뒤 schema realdb/generation/API/MCP/subscription CLI 계약 53 passed,26.66초,exit0. 실제 old-writer→0074 schema 검사를 포함한다. 원문은 review/kb-v3/final-integration-20261027/{contracts-red,contracts-green}.log.

실제 기존 구독 CLI L2는 합성 공개 Redis 패시지 1회만 호출했다. 이 Codex 중첩 명령 샌드박스에서 172ms에 failed to initialize in-process app-server client: Operation not permitted(os error1). 현재 엔진 계정 경로를 CLI에 전달했으며 인증 파일을 직접 조사하지 않았다. 같은 대상을 다른 엔진/명령으로 우회·재시도하지 않았다. 운영 가용성 실패로 일반화할 근거는 없으며 PDF/상충주장 실제 L2 E2E는 미완료다. l2-attempt.json/l2-real-probe.json을 보존했다.

fetch 결과 origin/main=1083a0ce,기준77c 대비 collection 단일도서관/0075 및 연구·운영 교정 등이 추가됐다. 이 중간 병합을 전체 최신 main 통합 또는 최종완료로 취급하지 않는다. 후속은 최신 main 통합, 보안·중복 계약 대조,실제 E2E,배포,운영 백필이다.
