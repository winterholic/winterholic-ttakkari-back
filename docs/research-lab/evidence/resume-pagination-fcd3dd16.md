# 500건 이후 연구 기록 탐색 인수

기준 backend `fcd3dd16`, frontend `0daa6c6bcd83ddcd9eefc1ddc1abb6dc2158e3c4`.
2026-10-26 리드가 중단된 작업 파일과 raw를 확인하고 실제 UI 인수부터 이어갔다.
새 독립 검증자·운영·모델/GPU 인수가 아니다. 최초 RED 명령/출력은 이번 중단
인계에 없어 인수 근거로 사용하지 않았다.

## 변경과 계약

기존 resume은 collection별 500행까지만 반환하고 다음 기록을 읽을 방법이 없었다.
기존 URL/기본값을 유지하면서 `offset>=0`, `1<=limit<=500`을 HTTP와 agent/MCP
resume에 전달한다. Node/Relation/Experiment/Event는 각자 created_at·id 순서로
페이지를 읽고 totals/has_more를 제공한다. 후반 페이지도 전체 연구의 일부이므로
truncated=true다. 해당 페이지 Node의 정확한 quote를 함께 반환한다.

인용의 active는 최고 revision 번호 대신 기존 KB의 최근 observation 판정을
사용한다. 이전 인용·조건은 변경하지 않는다. 새 DB/schema/collector는 없다.

기록 화면에 지연 로딩하는 ‘전체 기록 찾아보기’를 연결했다. 종류 선택,
이전/다음, 직접 페이지 이동, 총 건수, 마지막/빈 페이지, 재시도 및 이전 서버의
페이지 미지원 안내를 제공한다. 종류를 바꾸면 1페이지로 돌아간다.
관계 그래프·전 metric 차트나 calibration 구현으로 확대하지 않는다.

## 실제 검증

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_resume_pagination.py tests/test_research_lab.py tests/test_research_source_refresh_acceptance.py tests/test_research_selection_evidence.py -q --timeout=30 --tb=short
22 passed in 2.40s
db reset: 22 selective (avg 5.4/115 tables), 0 forced full, 0 fell back to full
$ pnpm exec vitest run src/pages/research
Test Files 5 passed; Tests 83 passed; Duration 3.61s
```

새 PG/API 검사는 collection당 505개 합성행을 포함하여 nodes506/relations505/
experiments505/timeline508을 100개씩 중복·누락 없이 읽고, 500 이후 인용,
범위 초과 빈 페이지, 잘못된 bounds 422, agent scope 404, 낮은 revision의
나중 observation까지 검사한다. 원본 [API raw](resume-pagination-fcd3dd16.json).
실제 DB identity는 supplied URL과 대조하고 운영 DB 이름을 거부했다.

새 Python 검사·fixture ruff 통과, 변경된 API/service/MCP의 기존 진단 증가0
(각16/3/0), diff check exit0. 프론트 build 출력은 중단 전 기록을 재사용했다.
로그 mtime이 변경된 5개 front source보다 늦음을 확인했다. lint 로그는 일부
source보다 빨라 그대로 재사용하지 않고 현재 source에서 다시 실행했다.

## 실제 브라우저 범위

설정된 ttakkari_browser만 사용했다. 연구 기록 화면에서 6페이지로 이동해
506번째 Node·정확한 한국어 quote·출처 링크와 SHA를 확인했다.
1440×1000과390×844에서 기록 영역을 계측했다. 모바일 archive는 x16/폭358,
페이지 입력과 버튼 높이44px이며 quote/hash가 줄바꿈됐다. 마지막 다음 버튼
비활성화, 종류 변경의 1페이지 복귀, timeline2건, relation0건 빈 상태를 확인했다.

- [desktop boxes](resume-pagination-desktop-fcd3dd16.txt), [mobile boxes](resume-pagination-mobile-fcd3dd16.txt)
- [timeline/reset](resume-pagination-timeline-fcd3dd16.txt), [empty](resume-pagination-empty-fcd3dd16.txt)
- [desktop quote](resume-pagination-desktop-quote-fcd3dd16.png), [mobile quote](resume-pagination-mobile-quote-fcd3dd16.png)

quick.mjs 미실행: 설정된 브라우저의 DOM box 계측을 사용했다. 전 UI 감사·
브랜드/가독성 점수·다른 화면 coverage로 확대하지 않는다. 전체 archive PNG는
내부 스크롤로 일부가 잘려 인용을 포함한 증거로 쓰지 않고 quote PNG를 따로 남겼다.
console의 auth refresh401 및 dev sw.js HTML MIME 관련3건은 로그인 전 fixture
오류다. 로그인 뒤 새 오류는 관측하지 않았으며 운영 PWA 검증이 아니다.

## 중단 복구와 경계

재개 때 backend18871 연결거부, 현재 주입 DB의 research_nodes 미존재를 확인했다.
이전 테스트 DB의 소실 원인은 확인하지 않았다. 운영 데이터 손실로 해석하지 않는다.
현재 테스트 DB에서 506건 fixture를 한 번 만들고 이전 study ID는 info의
previous_fixture에 보존했다. 같은 DB/ID가 있으면 재시드하지 않도록 helper를 보강했다.

첫 복구 서버의 health500은 prepare의 asyncio.run과 uvicorn의 서로 다른 loop에
연결 pool이 걸린 RuntimeError였다. prepare를 fixture lifespan의 같은 loop로
옮기고 자기 서버만 중단/재개한 뒤 ‘fixture reused’와 health200을 확인했다.
첫 서버 종료의 coroutine 경고는 보존하며 정상 종료로 바꾸지 않는다. 교정 후
브라우저 closed, 자기 backend SIGINT exit0. 이전 프론트18872는 응답 중인 서버를
재사용했으며 현재 도구의 프로세스 소유 handle은 확인하지 못했다.

프론트는 feature branch에만 push했다. backend 활성 후 main/Vercel 반영 순서를
유지한다. 기존 f8f8d559/fcd3dd16 예약은 중단 전 조회를 재사용하고 추가 poll하지
않았다. 새 검증 코드는 main 누적 push와 full SHA 예약으로 전달한다.
PMS7.4의 500행 이후 탐색 부분만 보완했으며 그래프/확대 metric 계측과 전체
R4·7·12~16, 운영·Metal·MCP 원래 완료 조건은 미완이다.
