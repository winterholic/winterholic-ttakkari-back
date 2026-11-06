# 로보틱스·AI 하네스 수집의 UI 분야 오인 교정

## 관측과 원인

- 운영 revision: `19d99b3f4d845af8c213a20ccea8df007450eac9` (`host_operation_start(runtime_report)`, 2026-11-06 08:15Z).
- 로보틱스 임무 `ec71b61e-19fa-4d46-a5f0-3ab468447c23`: 원 지시문은 로보틱스·피지컬 AI이나 저장된 분야는 interaction/components/enterprise이고 검색어 모두 디자인시스템이다. 적재 1,493건. 실제 결과 샘플에 Carbon Design System, Design System Tactics 등이 있다.
- 하네스 임무 `64299d2f-a776-4437-921e-124a7b859ebd`: hierarchy/responsive/interaction/components/tokens/ia로 오인. 적재 1,467건.
- 이전 `topics_in`은 부분 문자열만 비교한다. 실제 로보틱스 지시문은 interaction/motion/feedback, form(Transformer 등), erp(Hyperparameter)로 세 UI 분야에 매치된다. 세 분야 이상이면 UI 맥락 없이 `catalog.plan_parts`를 선택해 원 주제를 버린다. 하네스도 hierarchy/adaptive/feedback/form/토큰/navigation/taxonomy로 같은 오류가 발생한다.
- 실제 지시문 두 건을 fixture로 보존하고 수정 전 회귀 시험에서 오인 4건을 확인했다. 추가로 Bootstrap 통계·네이버/토스 금융 요청의 오인도 재현했다.

## 변경

- 분야 단어의 영문 경계를 확인하고 단순 복수형·accessibility를 보존한다.
- UI 분야 수만으로 카탈로그를 선택하지 않는다. UI 명시 맥락 또는 분명한 디자인시스템 이름이 있어야 한다. 통계 용어·기업 이름만으로 UI 대상을 선택하지 않는다.
- 긴 연구 지시문의 제목에서 검색 주제를 추출하고 백과사전 구축 지시서 같은 작업 문구를 제거한다. 원 지시문 전체와 요청한 평가·안전 등의 조사 축은 유지한다.
- 저장된 옛 오인 계획의 `rerun`은 부작용 전에 422로 거부한다. 원 지시문으로 새 임무를 시작하도록 안내하며 원래 계획·회차는 보존한다.

## 검증

- `PYTHONPATH=src:. <기존 개발 venv>/bin/python -m pytest -q unit_tests/test_collection_domain_routing.py unit_tests/test_collection_plan.py unit_tests/test_collection_research.py tests/test_collection_catalog_plan.py tests/test_collection_research_flow.py -k 'not script_shell_page'`: **55 passed, 1 deselected**, 임시 PostgreSQL DB 사용. 원 지시문 두 건·오인 반례·기존 디자인 임무·오인 계획 재실행 차단·비브라우저 연구 흐름 포함.
- 최초 통합 검사에서 기존 SPA 실브라우저 검사 1건은 `counts.imported == 1`에 실제 0으로 실패했다. 이 Codex 명령 환경의 수집 renderer 단계 관측이며 원인 미확인이다. 브라우저 실행을 반복하지 않았고 관련 없는 검사는 분리해 수행했다. 운영 브라우저 실패로 일반화하지 않는다.
- `git diff --check` 통과. 배포는 독립 컨트롤러의 검사·결과를 별도로 확인한다.

## 운영 조치와 남은 작업

- 위 오인 임무 두 건에 인증된 `POST /api/assistant/collect/missions/{id}/cancel {}` 수행. 응답은 모두 `cancelled`, 적재 수는 각각 1,493·1,467로 보존됨. KB 문서 삭제·재분류는 하지 않았다. 개별 문서가 모두 주제 밖인지는 미검증이다.
- 수정 revision 배포 후 원 지시문 그대로, `request_key=scope-repair:<원 임무 ID>`를 지정해 새 임무를 생성한다. 기존 이력과 새 커버리지를 섞지 않기 위한 별도 회차다. 입력은 fixtures/collect_robotics_goal.md 및 collect_harness_goal.md, 기본 예산 2,000쪽·깊이 3을 유지한다.
- 운영 계획 preview가 UI 질의를 생성하지 않는지 확인하고, 실제 수집 출처·적재·도서관 검색까지 검증해야 전체 완료다. 배포 예약만으로 완료 처리하지 않는다.

## 후속 운영 검증과 추가 교정 (09:11Z~)

- 첫 수정 `3e40791c`는 08:50Z에 활성화됐다. 08:58Z runtime_report와 인증된 plan preview에서 두 원문의 주제가 유지되고 UI 검색어가 사라진 것을 확인했다. 배포 `3ce99f0a`는 당시 draining이며 이전 세대 정리는 별개다.
- 복구 임무: 로보틱스 `c0307b64-0205-48a6-a10e-595ee2dd523a`, 하네스 `202305ec-a584-4998-87dd-c7e47cbc5384`. 생성 영수증은 `reports/collector/domain-recovery-20261106.json`.
- 09:11Z 실제 검증은 불합격했다. 로보틱스 acquired 145/imported 33, 하네스 imported 0. 두 준비 작업 모두 `policy_url is not a valid https URL: scheme_not_allowed`로 3회 소진했다. 로보틱스 trace에 Ubuntu·Git·관광 프롬프톤·태그 목록 등 무관한 후속 글도 확인했다. 두 복구 임무도 공식 cancel API로 중지했으며, 적재 33/0과 모든 이력을 보존했다.
- 라이선스 원인: `paper.license_from_url`은 HTTP Creative Commons URL을 허용 라이선스로 판정하지만 HTTP 원문을 그대로 반환했다. `register_document_license`는 HTTPS 정책 URL만 허용하고 `paper_phase`는 그 등록 오류를 격리하지 않아 전체 준비 트랜잭션이 되돌아갔다. 동일 호출 경로와 오류를 임시 DB 테스트로 재현했다.
- 탐색 원인: 검색 결과로 선택한 블로그 글 한 편도 max_depth 3과 여러 페이지 예산을 받아 같은 호스트의 모든 링크를 따라갔다. 검색 결과 웹페이지는 max_pages 1/max_depth 0으로 제한했다. 직접 지정한 URL과 카탈로그 루트는 기존 탐색 범위를 유지한다. 옛 검색 출처를 재실행할 때도 넓은 요청과 과거 캐시 frontier를 되살리지 않는다.
- 라이선스 교정: 인식된 Creative Commons URL만 HTTPS 형태로 사용하고 `reported_url`에 공급자 원문을 보존한다. 임의 출판사 URL을 바꾸거나 권한을 추정하지 않는다. 개별 문서의 정책 등록이 거부되면 savepoint로 격리하고 `paper_policy_invalid` gap과 서지 정보만 남겨 다른 논문 준비를 계속한다.
- 추가 재현 검사는 수정 전 실패를 확인했다. 변경 후 국소·임시 DB 검사 **101 passed, 1 deselected**, ruff 및 diff 검사 통과. 제외한 기존 SPA 실브라우저 검사는 위 기록과 동일하게 이 환경에서 미해결이다. 직접 지정한 URL의 링크 탐색과 옛 검색 출처 재실행의 캐시 격리도 별도로 확인한다.
- 다음 실행: 추가 수정본 배포 후 원래 지시문으로 `request_key=scope-repair-v2:<최초 임무 ID>`를 사용해 새 복구 임무 두 건을 시작한다. 실제 주제에 맞는 적재 문서와 KB 검색을 확인하기 전에는 목표를 완료로 표시하지 않는다.
