# GitHub 탐색 API 계약 fixture (ADR 0042)

`api/*.json` = 각 엔드포인트의 `data` 값(백엔드 골든 테스트 `unit_tests/test_github_views.py`가 이 파일과 일치하는지 검사한다).
`upstream/*.json` = 상류 GitHub 응답 샘플(백엔드 테스트 전용, 프론트는 쓰지 않는다).
타입 정의의 정본은 `src/ttakkari/github/schemas.py`(pydantic). 프론트 타입은 이 파일들과 같게 만든다.

## 공통

모든 경로는 `/api/github` 아래, 소유자 Bearer 인증(기존 앱과 동일). **`account=<login>` 쿼리 필수**(`accounts`·`resolve` 제외).

봉투(`accounts`·`resolve` 제외 전부):

```json
{ "data": <아래 fixture>, "page": {"page":1,"per_page":30,"has_more":false,"total_count":null,"limit_hit":false} | null,
  "rate": {"limit":5000,"remaining":4990,"reset":1792291172,"resource":"core","low":false} | null,
  "fetched_at":"2026-10-18T01:00:00+00:00", "cached":false, "stale":false, "account":"winterholic" }
```

- `page`는 목록 응답에만 있다. `has_more`로 "더 보기"를 판단한다(개수로 판단하지 말 것: 이슈는 PR을 걸러내 페이지당 개수가 줄 수 있다). 다음 페이지는 `page+1`.
- `stale:true` = 상류 실패(한도·타임아웃·5xx) 때문에 만료된 캐시를 대신 낸 것. 화면에 "오래된 데이터" 표시.
- `rate.low` = 남은 호출 100 이하.

오류 본문(앱 공통 `{code,message}` + 추가 필드). **상류 401은 절대 401로 오지 않는다**(`gh_auth_expired`=502).

| code | HTTP | 추가 필드 | UI |
|---|---|---|---|
| `no_account` | 503 | | gh 로그인 안내 |
| `account_unavailable` | 404 | | 계정 선택 화면 |
| `invalid_target` / `unsupported_host` | 422 | | 입력 오류 |
| `ref_not_found` | 404 | | 브랜치/태그/커밋 없음 |
| `readme_not_found` | 404 | | README 없음(빈 상태) |
| `gh_not_found` | 404 | | 없음 또는 권한 없음(구분 불가: 비공개는 404) |
| `gh_forbidden` | 403 | `reason`: `sso_required`(+`sso_url`)·`scope_missing`·`policy` | 권한 부족 |
| `gh_auth_expired` | 502 | | 만료: Mac Studio 재로그인 |
| `gh_rate_limited` | 429 + `Retry-After` | `reset`(epoch 초), `retry_after`(초) | 한도 카운트다운 |
| `gh_gone` / `gh_unavailable_legal` | 410 / 451 | | |
| `gh_unprocessable` | 422 | | GitHub에서 열기 |
| `gh_empty_repository` | 409 | | 빈 저장소 |
| `gh_too_large` | 413 | | GitHub에서 열기 |
| `gh_timeout` / `gh_upstream_error` / `gh_schema_changed` | 504 / 502 / 502 | | 오류+재시도 |
| `gh_logs_unavailable` | 502 | `reason` | 로그 안내(미지원 상태) |
| `validation` | 422 | `errors` | 쿼리 형식 오류(기존 앱 형식) |

## 엔드포인트 ↔ data fixture

| 경로 | 쿼리 | data |
|---|---|---|
| GET `/accounts` | `probe=true`(선택) | 봉투 없음: `{"accounts":[AccountOut], "gh_available":bool, "fetched_at"}` — `account` 불필요. gh를 못 찾으면 `accounts:[]`+`gh_available:false`(500 아님) |
| GET `/resolve` | `q`, `account`(선택) | 봉투 없음: `ResolveOut`. tree/blob URL의 ref에 `/`가 있으면 account가 있어야 실제 브랜치로 해석, 없으면 `needs_ref_resolution:true` |
| GET `/me` | | `api/me.json` |
| GET `/me/orgs` | page, per_page | `api/orgs.json` |
| GET `/me/repos` | sort(updated\|created\|pushed\|full_name), page, per_page | `api/repos.json` |
| GET `/owners/{login}` | | `api/owner_org.json` (user/org 공통 Profile, `type`으로 구분) |
| GET `/owners/{login}/repos` | sort, page, per_page | `api/repos.json` |
| GET `/repos/{owner}/{repo}` | | `api/repo.json` |
| GET `/repos/{owner}/{repo}/branches` | page, per_page | `api/branches.json` |
| GET `/repos/{owner}/{repo}/contents` | `ref`, `path` | 디렉터리 `contents_dir.json`, 파일 `contents_file.json`/`_binary`/`_huge`, `contents_symlink.json`, `contents_submodule.json` |
| GET `/repos/{owner}/{repo}/readme` | `ref` | `api/readme.json` |
| GET `/repos/{owner}/{repo}/pulls` | state(open\|closed\|all), sort(created\|updated), direction, page, per_page | `api/pulls.json` |
| GET `/repos/{owner}/{repo}/pulls/{number}` | | `api/pull.json` |
| GET `/repos/{owner}/{repo}/pulls/{number}/files` | page, per_page | `api/pull_files.json` (`page.limit_hit`=3,000개 상한) |
| GET `/repos/{owner}/{repo}/issues` | state, labels(쉼표), sort(created\|updated\|comments), direction, page, per_page | `api/issues.json` |
| GET `/repos/{owner}/{repo}/issues/{number}` | | `api/issue.json`; PR 번호면 `api/issue_as_pull.json`(`kind:"pull"` → PR 화면으로 이동) |
| GET `/repos/{owner}/{repo}/issues/{number}/comments` | page, per_page | `api/issue_comments.json` |
| GET `/repos/{owner}/{repo}/actions/runs` | status, branch, event, page, per_page | `api/runs.json` (`page.total_count`) |
| GET `/repos/{owner}/{repo}/actions/runs/{run_id}` | | `api/run.json` |
| GET `/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` | filter(latest\|all), page, per_page | `api/jobs.json` |
| GET `/repos/{owner}/{repo}/actions/jobs/{job_id}/logs` | from_line(1~), lines(1~2000) | `LogWindow` (schemas.py) |

## 화면이 숨기면 안 되는 플래그

- 파일: `file.binary`(미리보기 없음), `file.too_large`+`file.truncated`(앞부분만 표시), `file.too_large` 단독(본문 없음), `kind: symlink|submodule`(대상 안내), 디렉터리 `entries_limit_hit`(목록이 전부가 아닐 수 있음).
- PR: `mergeable_computing`(계산 중, 잠시 후 다시), `body_truncated`, 파일별 `patch_state`(`ok|empty|unavailable|truncated`), `page.limit_hit`.
- 이슈·댓글: `body_truncated`. 로그: `truncated`(창 축소), `masked`(비밀 마스킹 적용).
