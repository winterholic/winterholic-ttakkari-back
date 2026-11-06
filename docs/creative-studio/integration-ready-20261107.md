# Creative backend 통합 후보 — 2026-11-07

상태: 최신 원격 main 위에 로컬 통합 완료. push·배포하지 않았으며 독립 최종 검증과 전체 제작 목표는 남아 있다.

## 영수증과 기준 SHA

작업 전 지정한 세션 목록에서 이번 작업 프롬프트와 일치하는 running Run을 골랐고 상세 GET으로 프로필을 확인했다. 목록 첫 항목을 선택하지 않았다.

```text
/Users/winterholic/bin/ttk get /api/sessions/d93c9153-076c-4479-9a65-3b61de244221/runs
→ matching running Run: d500d2a3-1af0-4afa-ba04-8d7a4e386ad6
/Users/winterholic/bin/ttk get /api/runs/d500d2a3-1af0-4afa-ba04-8d7a4e386ad6
→ status=running, profile=회사 코덱스 ai2 (ai2@hyperez.io)
```

착수 d0 후보는 `d0f7a24430218f1d79c91b57407e91673dcb1f12`, 브랜치 `feat/creative-production-20261107`였다. 기존 후보 경로는 tracked 변경 없이 7개의 untracked render/benchmark 디렉터리·파일을 가지고 있었다. 이를 수정·정리하지 않았다. 기존 `-verify`는 a398 SHA, `-static-verify`는 d0 SHA의 detached clean worktree였다.

## 원격 확인과 격리 통합

remote는 `https://winterholic@github.com/winterholic/winterholic-ttakkari-back.git`, GitHub owner는 `winterholic`이다. 지원 `github_accounts`에서 `winterholic`과 `daeseon-dev`를 확인했고 owner 계정의 토큰을 해당 명령에만 전달했다. `gh auth switch`, Keychain 직접 읽기, 토큰 출력은 하지 않았다.

```text
GH_TOKEN="$(gh auth token --hostname github.com --user winterholic)" gh api user --jq .login
→ winterholic
GH_TOKEN="$(gh auth token --hostname github.com --user winterholic)" gh repo view winterholic/winterholic-ttakkari-back --json nameWithOwner,url
→ winterholic/winterholic-ttakkari-back https://github.com/winterholic/winterholic-ttakkari-back
GH_AS_USER=winterholic GH_AS_TOKEN="$(gh auth token --hostname github.com --user winterholic)" git -c credential.helper= -c 'credential.helper=!f() { echo username=$GH_AS_USER; echo password=$GH_AS_TOKEN; }; f' fetch origin main
→ From https://github.com/winterholic/winterholic-ttakkari-back; main fetched
```

fetch 당시 `origin/main` full SHA는 `293bb8440fb96aaf648a59c7c821a99348ce325c`였다. d0와 공통 조상은 `5f5cbd997addcb4ea084ab908790f8f6eec54122`; 원격에는 조상 이후 44개, d0에는 5개 커밋이 있었다. 두 변경 구간의 경로 교집합은 0개였다. 따라서 충돌 경로나 수동 충돌 해결은 없다.

```sh
git worktree add -b integ/creative-production-20261107 \
  /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107-integrate origin/main
git -C /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107-integrate \
  merge --no-ff --no-commit d0f7a24430218f1d79c91b57407e91673dcb1f12
# Automatic merge went well; stopped before committing as requested
git commit -m "merge: integrate Creative production candidate on latest main"
→ merge SHA 4c76658d7a68f5607c2bbbd24c7f2c2e51ff2104
```

merge commit의 부모는 원격 SHA와 d0 SHA이며 두 SHA 모두 `git merge-base --is-ancestor <sha> HEAD`로 확인했다. 원격 main의 다른 변경도 그대로 포함된다. 특히 `docs/api-contract.md`, `src/ttakkari/runs/manager.py`, `src/ttakkari/generation_service.py`, `src/ttakkari/executor.py`는 통합 commit에서 origin/main과 바이트 단위로 동일했다. API 계약 문서가 원격에서 갱신된 내용도 보존됐다.

새 격리 경로는 `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107-integrate`, 전용 브랜치는 `integ/creative-production-20261107`이다. `origin/main`에서 새 clean worktree를 만든 뒤 d0를 병합했다. 기존 main, d0 후보, 두 검증 worktree 및 별도 frontend는 수정/reset하지 않았다.

## 계약 보존 점검

검토는 새 통합 worktree의 코드와 부모 트리 기준으로 했다. 제품/API 테스트는 다시 실행하지 않았다.

- Creative v2 라우터는 기존 `User` 인자 인증 경계를 사용하고 `main.py`의 기존 라우터 등록을 유지한다. 다운로드용 계획 한정 토큰 경로도 기존 구현 그대로다.
- Creative 자산/렌더 소비자는 기존 `generation_service`의 `_creative_assets`와 `maintenance_jobs` 및 비세대 실행의 `start_legacy_queued_consumers` 경로에 남아 있다. `asset_worker`/`render_worker`는 기존 `queued_work` claim·drain 경계를 사용한다.
- `queued_work.begin_drain`, inflight 대기 및 executor의 기존 drain 절차를 바꾸지 않았다. 승인 revision/hash, production durable record, retry/cancel 상태와 remote API의 deferred executor 계약을 수정하지 않았다.
- 정적 확인 명령에서 `docs/api-contract.md`, `runs/manager.py`, `generation_service.py`, `executor.py`는 origin/main과 동일했고, Creative 라우터·worker·테스트 bytes는 d0와 동일했다.

## source hash 기반 검증 재사용

다음 지문은 경로와 Git blob SHA를 정렬해 결합한 manifest SHA-256이다. 56개 경로에는 `src/ttakkari/creative/v2/`, Creative 전용 unit 회귀, HyperFrames/3D 테스트와 benchmark 스크립트가 포함된다.

| 기준 | 파일 수 | manifest SHA-256 | 사용 |
|---|---:|---|---|
| a398 `a3980e6b043df9c74a279f6717fc2e2ad9670e72` | 56 | `43c18320cfc784080edb8a95e618554232c90ecfdcc0102c2b6379f53972dc5e` | 그 시점의 유효한 변경 불포함 검증만 재사용. 기록된 통합 회귀 `244 passed, 1 failed, 4 deselected`를 전체 통과로 취급하지 않음 |
| d0 `d0f7a24430218f1d79c91b57407e91673dcb1f12` | 56 | `4a3b684b0be619be28eb3e415269ca890311aae757af77ec6939531d850ea41f` | 아래 정적 교정 검증의 기준 |
| 통합 merge `4c76658d7a68f5607c2bbbd24c7f2c2e51ff2104` | 56 | `4a3b684b0be619be28eb3e415269ca890311aae757af77ec6939531d850ea41f` | d0와 일치하므로 d0 증거 재사용 가능 |

d0와 통합 tree 사이의 위 기능 manifest가 동일해 merge 때문에 달라진 Creative 기능 bytes는 없다. d0 정적 교정 기록의 변경 직접 회귀는 `47 passed, 2 deselected`; Ruff는 `All checks passed!`, Python AST parse도 통과했다. 해당 지문이 동일해 이를 통합 후보의 소유 변경 회귀 증거로 재사용한다. Stage2/3와 기존 API/consumer 경로도 d0 검증에서 수정되지 않았으며 이 통합에서 bytes가 달라지지 않았다. 통합 branch에서 신규 Creative 테스트를 실행하지 않았다.

```sh
git rev-parse 293bb8440fb96aaf648a59c7c821a99348ce325c
git rev-parse d0f7a24430218f1d79c91b57407e91673dcb1f12
git rev-parse 4c76658d7a68f5607c2bbbd24c7f2c2e51ff2104
# 마지막 문서 커밋 이후 검토 후보 전체 SHA:
git -C /Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107-integrate rev-parse HEAD
```

통합 범위의 Python source·전용 regression·비 evidence 문서에서 `git diff --check`는 추가 진단 없이 종료했다. 원 후보에서 이미 문서화된 whitespace 예외는 원문 evidence log/patch와 GSAP vendor 파일뿐이다. 원문이나 vendor bytes를 수정하지 않았다.

## 하지 않은 검사와 남은 검토

| 항목 | 결과 |
|---|---|
| 기존 `a398`/`d0` 검증 | source hash가 동일한 기존 증거만 재사용. 새 전체 회귀는 실행하지 않음 |
| npm EPERM 검사 | 알려진 sandbox 접근 오류이므로 재호출하지 않음 |
| A browser 검사 | 알려진 IPC 차단이므로 실행하지 않음 |
| C bpy/preflight 검사 | 알려진 환경 실패가 있어 재실행하지 않음 |
| 신규 모델 호출 또는 producer render | 실행하지 않음 |
| 유음 조립 | 미완. 승인된 기존 영상/음원 계획 기반 최종 조립 및 machine QA가 남음 |
| UI·실제 사용자 여정 | 미검증. 별도 frontend 작업 결과와 대조 필요 |
| 운영·원격 executor 소비 | 미검증. 배포 상태나 운영 동작을 주장하지 않음 |
| 독립 의미/시청/청취 및 품질 평가 | 미검증. machine 계약 검사로 대체하지 않음 |
| push·배포 예약·서비스 재시작 | 하지 않음. 독립 최종 검증 전 후보 보고 단계 |

## 최종 후보 조회

이 문서를 포함한 최종 candidate SHA는 전용 worktree에서 `git rev-parse HEAD`로 확인한다. `git show -s --format='%H %P %s' HEAD`로 원격 main과 d0를 포함한 부모 관계를 조회할 수 있다. 작업 branch와 경로는 위 통합 경로이며, 최종 clean 상태를 위해 이 문서도 전용 branch에 커밋한다.
