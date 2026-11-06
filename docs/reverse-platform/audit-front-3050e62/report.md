# Front 3050 × Core 7266 독립 detail API 소비 감사

## 판정

**부분 완료.** 고정된 backend의 임시 PostgreSQL ASGI API와 frontend 경로 조립을 연결한 독립 probe는 통과했다. 로그인된 브라우저 화면은 관찰하지 못했으므로 화면별 렌더·overflow·키보드 조작은 미검증이다. 이 결과는 fixture 기반 API/소비 감사이며 실제 모델, 운영 E2E, 전체 제품 감사를 뜻하지 않는다.

## 고정 대상과 범위

| 항목 | 고정값·결과 |
|---|---|
| backend 제품 소스 | `7266a2a19332935a6e32d10108a7e021956ef910` (감사 worktree HEAD) |
| frontend 소비자 | `3050e62cf94bed537b6aa5c440273e9034986e0e` |
| 기존 core 감사 | `back-reverse-audit-core-20261107/docs/reverse-platform/audit-core-7266a2a1/` 보존, 수정·재실행 안 함 |
| 기존 독립 초안 | 본 경로의 untracked `probes/test_front_detail_contract.py` 하나를 이어서 수정. 기존 감사/core probe 및 제품 소스·구현자 테스트는 수정하지 않음 |
| 제품 diff | backend/frontend 모두 없음. 새 파일은 감사 경로의 probe와 이 보고서뿐 |

## 실제 API 응답 probe

`tests/conftest.py`의 프로세스별 임시 PostgreSQL·테스트 설정과 고정 core probe의 fixture helper를 재사용했다. `manager.dispatch`는 no-op이라 외부 agent/실제 모델을 부르지 않는다. 구현·verifier report fixture는 임시 작업 폴더에 기록했다. 운영 DB, 서비스 재시작, 비밀 파일, 인증 우회는 사용하지 않았다.

실행:

```text
PYTHONPATH=src:tests:/Users/winterholic/development/projects/winterholic-ttakkari/back-reverse-audit-core-20261107/docs/reverse-platform/audit-core-7266a2a1/probes \
  /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q \
  docs/reverse-platform/audit-front-3050e62/probes/test_front_detail_contract.py
...                                                                      [100%]
3 passed in 1.50s
```

검증한 내용:

- verifier 필수 검사 실패, verifier 직접 비교 1건, verifier 직접 비교 0건을 각각 실제 ASGI detail 응답으로 읽었다. 세 상태 모두 독립 검증은 `failed`였다. 비교 0건은 verifier 전용 `verification_direct_comparison.status=unverified`, `count=0`, `verification_comparisons=[]`로 확인했다.
- 구분 기준: `result_state.direct_comparison`은 구현 회차의 비교를 나타내고 verifier 비교는 `verification_direct_comparison`에 별도로 남는다. 첫 probe 실행에서 둘을 같은 값으로 기대해 2건이 실패했고, 코드와 실제 응답을 대조한 뒤 테스트 기대를 분리했다. assertion을 실제 관측에 맞춰 없애지 않고 각 의미 필드를 확인한다.
- 각 실패 상태에서 `result.latest == latest`, `result.history == history`, source/check log 및 `result_state`의 구현 결과가 같은 snapshot을 참조했다.
- API JSON의 session UUID를 문자열로 정규화해 비교했다. 구현 session ID와 verifier session ID는 달랐고, verifier `verification_run_id`는 구현 `run_id`와 달랐다.
- 실제 API의 `output_path`, report `source.files`, `execution.checks[].log`를 UI의 `output_path + relative path` 방식으로 `/api/artifacts/resolve`에 전달했다. 응답 artifact의 원본 content API에서 `src/main.py`와 `probe.log`의 fixture 본문을 확인했다.
- 성공 상태 retry는 사용하지 않았다. verifier 필수 검사 실패로 `partial` 상태를 만들고 DB commit 뒤 retry를 호출해 `202`를 받았다. retry 직후 latest는 null, history는 유지, verifier session은 null, result_state verification은 pending이었다.

## Front 소비 경로 소스 확인

- `Reconstruction.tsx`는 `reverseV2FromDetail(job)`에서 latest/history를 소비하며 직접 비교 요약과 독립 검증 상태를 별도 표시한다. 독립 검증 Run ID 및 verifier session 링크도 서로 다른 필드에서 읽는다.
- 소스 미리보기와 check log 버튼은 report 상대 경로에 `job.output_path`를 접두한 뒤 resolver API를 호출한다. 위 probe가 이 경로를 실제 fixture API 응답에 연결해 검증했다.
- 소스 기준 확인으로 `verification_direct_comparison`과 verifier 전용 check log를 상세 화면이 직접 표시하지는 않는다. 실패 표시는 독립 검증 상태와 verifier 대화 링크에 의존한다. 이는 코드 관찰이며 브라우저에서 화면으로 확인한 결과가 아니다.

## 브라우저 관찰

frontend 고정 worktree에서 다음 개발 서버를 시작했다.

```text
TTAKKARI_NO_ENV_FILE=1 VITE_API_BASE=http://127.0.0.1:3051 npm run dev -- --host 0.0.0.0 --port 3050
VITE v8.3.3 ready in 136 ms
Local: http://localhost:3050/
```

설정된 `ttakkari_browser`로 `/reverse`에 이동했다. browser 도구 자체는 정상 실행했으나 격리 프로필이 `/login?next=%2Freverse`로 이동했고, 화면에 `Mac Studio 연결 끊김` 및 비활성 로그인 버튼이 표시됐다. 인증 우회 금지에 따라 로그인이나 API mock 인증을 만들지 않았다.

| viewport | 관측 | 판정 |
|---|---|---|
| 390×844 | 접근성 snapshot은 login 화면 전체 폭 390 CSS px, 주 콘텐츠 x=16/w=358. 목록·상세·비교·실패·미검증·소스·이력·후속 액션은 도달하지 못함 | 해당 UI 요구 미검증 |
| 1280×900 | 접근성 snapshot은 login 화면 전체 폭 1280 CSS px, 중심 패널 x=422/w=435. 상세 UI에는 도달하지 못함 | 해당 UI 요구 미검증 |

브라우저는 도구로 닫았다. 로컬 Vite 서버 종료 시 관측한 출력은 다음과 같다.

```text
SecurityError: Failed to register a ServiceWorker ... /sw.js ... unsupported MIME type ('text/html')
```

따라서 이 오류는 해당 로컬 Vite 테스트 화면에서 관측한 service worker 오류로만 기록한다. 390px/1280px의 상세 화면 overflow, 비교·실패 렌더, 키보드 흐름 및 시각 평가는 검증하지 않았다. 브라우저 route fixture/mock은 실행되지 않았다.

## 실행하지 않은 범위

- 기존 core 5개 probe와 frontend Vitest 22개: 카드 지시에 따라 재실행하지 않음.
- 로그인 상태의 390px·1280px 상세 UI 및 키보드 조작: 브라우저 격리 세션에 로그인 연결이 없어 미실행.
- 실모델, 운영 DB/API, 운영 E2E, 배포 revision 및 전체 감사: 이 fixture 감사 범위 밖이며 미실행.

## 재현 및 저장

저장된 probe: `docs/reverse-platform/audit-front-3050e62/probes/test_front_detail_contract.py`

```bash
PYTHONPATH=src:tests:/Users/winterholic/development/projects/winterholic-ttakkari/back-reverse-audit-core-20261107/docs/reverse-platform/audit-core-7266a2a1/probes /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q docs/reverse-platform/audit-front-3050e62/probes/test_front_detail_contract.py
git diff --check
```

이 감사가 추가한 보고서와 probe만 감사 경로에 커밋했다. 제품 소스에는 변경이 없다.
