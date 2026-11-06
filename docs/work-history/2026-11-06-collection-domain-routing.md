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
