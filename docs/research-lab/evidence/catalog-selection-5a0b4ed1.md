# 카탈로그 방법 검토와 운영 데이터 보존

2026-10-26 리드 Codex 직접. 기준 backend5a0b4ed1/frontend b2b3600.
R1~16/PMS664a2c29 전체 완료가 아니다. 새 작업자는 만들지 않았다.

## 제품 변경

기존 `/studies/{id}/select`는 연구 Node(kind=method)만 조회했다. Registry에만
있는 Method는 source_node_id가 없으면 선택·실패 이력 검토에서 빠졌다.
기존 동작을 기본값 `candidate_source=research`로 보존하고 `catalog`를 추가했다.
같은 scope+system의 Method를 최대200개 조회하며 기존 조건/실패/상충 판정을
재사용한다. 원본 Method·Node·Run을 수정하거나 가짜 Node를 생성하지 않는다.

Method ID와 compared_method_ids로 연결된 실패는 원래 오류·Run 상세·선언/관측
조건을 유지한다. 같은 조건/불명 조건은 검토 대상으로 남기며 다른 조건의 실패로
전역 배제하지 않는다. source Node의 근거와 관계도 scope를 확인해 연결하고,
관계의 양 끝 조건과 방향을 보존한다. 후보/관계/실패의 조회 한계는 truncated로
공개한다. source Node가 없으면 비용과 적용 조건을 추측하지 않는다. 카탈로그의
hardware/runtimes 합집합을 임의의 조건 조합으로 변환하지 않는다.

호스트 실행 지원 여부는 기존 readiness의 계산 결과를 별도 반환한다. 검토
우선순위는 실행 보증이나 효과 점수가 아니다. 근거 질·정보가치는 unassessed,
효과는 unknown_until_measured를 유지한다. 수치 calibration을 구현했다고 하지 않는다.

라이브러리의 ‘현재 연구에 맞춰 검토’는 저장한 연구 환경과 검색어·시간 예산으로
호출한다. 목록의 다른 축 필터가 이 검토에 적용되지 않는다는 설명을 둔다.
미측정 비용/조건 부족·다른 조건의 실패·현재 상충 보고를 구분하며 원본 실패
분석과 방법 상세로 이동한다. 구 서버가 catalog 요청을 거부하거나 응답 계약1을
주지 않으면 지원 안내/오류를 표시한다. 일반 예산 검증 오류를 구 서버로 오인하지 않는다.

## 검증

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_catalog_selection.py -q --timeout=30 --tb=short
첫 반례 2 failed in0.74s: candidate_source422 / helper 미구현
직접 호출 seed 반례 1 failed,3 passed in1.08s: 빈 seed catalog
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_catalog_selection.py tests/test_research_selection_failures.py tests/test_research_selection_context.py tests/test_research_selection_evidence.py tests/test_research_source_refresh_acceptance.py tests/test_research_lab.py unit_tests/test_research_rules.py -q --timeout=30 --tb=short
67 passed in3.58s
$ .venv/bin/ruff check src/ttakkari/research/service.py src/ttakkari/api/research.py src/ttakkari/orchestrator/mcp_server.py tests/test_research_catalog_selection.py tests/research_smoke/catalog_selection_live.py
All checks passed!
```

API/RunPG 검사는 카탈로그만 있는 방법·scope 비노출·조건 A/B/불명·실패/상충·
상대 Node 조건·200상한·직접 seed 초기화·기존 Node 기본값과 선택 event 보존을
포함한다. 추가 Node 시험의 summary 누락은 준비 단계 NOT NULL 실패로 교정했다.
첫 프론트 시험은 미구현 컴포넌트 import 실패였다. 후속 research 전체/tsc·VitePWA·
변경eslint와 design-lint를 실행했다. 최종 수치/소스hash는 같은 이름 JSON에 보존한다.
서로 겹치는 52/67 검사나 프론트 부분/전체 검사를 합산하지 않는다.

## 실제 브라우저와 저장

현재 Run이 제공한 격리 DB의 database/user identity를 대조했다. fixture 계정과
합성 Method/실패 Run을 사용하는 localhost HTTP18881/프론트18882, lifespan
background off 환경이다. 운영 계정·모델·GPU를 사용하지 않았다.
1280px와390px에서 버튼→검토→실패 원문→분석 화면의 같은 Run/오류를 관측했다.
모바일 검토 영역은 x16/width358로390px 안에 있고 버튼 높이는44px였다.
두 실제 browser POST의 selection event를 별도 프로세스 fresh ASGI resume에서
읽어 같은 method/run ID·eligibility·오류를 확인했다. 스크린샷은
catalog-review-desktop.png / catalog-review-mobile.png, 원시 resume는 JSON이다.

방법 상세 클릭은 URL의 method slug 변경만 관측했다. dialog snapshot은 렌더
이전 요청으로 미확인이다. 기존 방법 상세 인수를 새 인수라고 재표현하지 않는다.
콘솔의 로그인 전 refresh401와 Vite dev sw.js text/html 등록 오류를 구분한다.
배포 PWA 오류나 콘솔 오류0으로 주장하지 않는다. 브라우저와 두 시험 서버는 닫았다.

첫 standalone fixture는 이미 있던 시험 credential insert로 시작이 거절됐다.
이어 독립 PostgreSQL initdb가 Codex 명령 환경에서 exit1이었다. 실패 경로를
재시도하지 않고 제공된 Run DB/기존 fixture 계정을 명시적으로 사용했다. 준비
오류는 호스트/운영 장애가 아니다. 첫 readback의 system python은 httpx가 없어
HTTP 호출 전에 실패했고, 저장 대조는 저장소 venv의 fresh ASGI로 실행했다.

## 운영 활성과 보존

curl api/version과 runtime_report3cef9956은 활성 d5bf5df7/PID10029를 반환했다.
4a36d067 controller succeeded의 실제 로그119행은5821 passed/19 skipped/
2 xfailed/5020 subtests,1340.86s다. 이어 migration0072~0075 기록이 있다.
이는 d5bf 범위의 전체 gate이며 이번 새 카탈로그 기능의 배포 gate가 아니다.

활성 후 운영 MCP에서 기존 eb41 연구12nodes/9relations/0experiments/45events/
10evidence·truncatedfalse를 복원해 기준600eb843의575필드 변경/누락0을 확인했다.
같은 운영 A 두 Run의 raw/env·각5측정·artifact와 채택 study/decision/resume가
fcd3 저장 증거와 같았다. 새 벤치를 실행하지 않았다. 원본 개인 연구 본문은
저장소에 복제하지 않고 hash 비교와 runtime만 operating-readback-d5bf5df7.json에 보존했다.
이는 전체 운영 DB row/lock시간·다른 scope 검증이 아니다.

운영 브라우저는 login?next=/research와 refresh401을 반환했다. 인증 운영
화면은 미확인이다. 수치 calibration/자동 collector 추출·native checkpoint·
전체 memory·GPU owner crash/legacy92/PMS7.x의 나머지 원조건도 미완료다.
새 기능의 누적 배포/프론트 게시 결과는 최신 manage_work가 소유한다.

## 23:30 재개 검증

백엔드 5개 소스 hash는 기존67검사 영수증과 일치했다. 프론트 api/test 2개는
이전94검사 후 배포 시차 안내가 추가되어 hash가 달랐다. 기존94결과는 옛 hash
범위로 보존하고 현재 `npx vitest run src/pages/research/catalog-selection.test.tsx`
4 passed(662ms), 변경4파일 eslint exit0, `npm run build` tsc/Vite/PWA exit0를
실행했다. frontend57730bd. 기존 chunk-size warning은 남아 있다.

b6444f6a controller는5a0b4ed1에서1407 passed/16 skipped 후 게임 스튜디오
설치 Docker mount source mkdir 오류(exit127)로 실패했다. 운영 Research 오류로
확대하지 않는다. ops 원문은 deploy-mount-failure-b6444f6a.json에 보존하며
긴87행은 ops의 행 길이 제한으로 잘려 끝 errno는 여전히 미확인이다.
