# Creative Studio v2 첫 수직 슬라이스 (2026-10-22~23)

범위: 요구사항 정본 [creative-studio-v2-requirements.md](../design/creative-studio-v2-requirements.md)의 WBS 8.2~8.6 첫 단위. 전체 요구사항 완료가 아니다. main 병합·배포 없음. 검수용 브랜치 push까지.

- backend: `feat/creative-multidimensional-v2` (시작 origin/main 75a12d6, 작업 중 f69ee98로 fast-forward. 늘어난 커밋은 테스트와 문서뿐)
- frontend: `feat/creative-intent-duration` (시작 aa9049c, origin/main 변동 없음)
- 공유 back/front 체크아웃은 건드리지 않았고, `feat/creative-engines-director`의 director.py·pipeline.py·managed_comfy.py·records 작업과 겹치는 코드는 쓰지 않았다.

## 만든 것
- C01~C12 카탈로그 166개 항목, 능력 55개, 도구·모델·스킬·레시피 레지스트리, R01~R20 시드와 능력 행렬(코드 생성 문서).
- 호스트 실측 탐침(`probe.py`)과 스냅샷. 스냅샷 값: 탐침 24/28 설치, lab 기록 21건, 스킬 초안 21개. Blender GUI·Godot·R3F·시스템 ffmpeg는 없음.
- 의도 계약(IntentSpec)과 검증, 충돌·대안·필수/선호 매칭, 근거 추적, 재현 가능한 fingerprint.
- 구현은 Style DNA 16필드(사용자 원문 15개 필드 + 설계상 `version` 추가), 이미지·영상·코드·렌더 설정·성공/실패 참조, 버전 불변 저장소, 상속(override 사유 강제), 합성과 명시적 충돌 해결, 작품 단위 고정을 포함한다. 16개 필드 자체를 사용자 지시로 귀속한 기록은 2026-10-23 정정했다. `version` 필드는 버전 고정을 명시적으로 저장하기 위한 구현 선택이다.
- 통합 길이 정책(자동 + 선택적 희망, soft/strict 분리), 발화 추정, 프레임 배분, 편집 후 재계산, 계획 저장·편집·승인 API.
- 프런트: 길이 선택(자동·프리셋 8·직접 1~900초·맞춤 방식), 제작 계획 화면(예상 러닝타임·이유·필요 길이 범위·장면 편집·재계산·판정·승인), v1 생성 폼의 길이 선택 교체.

## 실행 증거
- backend `pytest`(creative 전체 + 라우트 인벤토리): 168 passed. v2 전용은 duration 29, registry 15, matching 17, style_dna 16, plans 13, api 6.
- 변이 시험 5건(희망 길이 clamp 제거, exact 충돌 무시, 둘 다 필수 충돌 무시, 스타일 충돌 첫 값 우세, installed를 executed로 승격)을 일부러 넣어 모두 테스트가 실패하는 것을 확인하고 원복했다.
- frontend `vitest` 전체 1304 passed 10 skipped(v2 길이·선택·계획 화면 38건 포함), `tsc -b`·`eslint .`·`ui-lint`·`pnpm build` 통과.

## 미완료·미검증
- 렌더 연결 없음(8.7). 실제 생성 영상 0편.
- 서버 LLM 의도 추출기 없음. 계약과 검증만 있다.
- UI 브라우저 스크린샷 미실행(jsdom만).
- 전문 능력(anim.expression, gen.layer_separation, research.fact_check 등 차단 10개 레시피의 누락 능력) 구현은 후속.
- 플랫폼 상한은 YouTube Shorts만 근거 있음.
