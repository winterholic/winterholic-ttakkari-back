# ADR 0042: 모바일 GitHub 탐색 페이지 (읽기 전용)

- 상태: 설계 확정(2026-10-18, 구현 전). 사용자 요청("따까리에 모바일용 GitHub 조회 페이지")에 따른 에이전트 설계이며 구현·배포는 별도 지시를 따른다.
- 실행 근거: PMS `winterholic-ttakkari` WBS 20 `[GITHUB]` 트리(GH00~GH06). 이 ADR이 각 작업의 선행 설계 경로다.
- 쓰기(댓글·머지·재실행·이슈 생성)는 이 ADR의 범위가 아니다. 필요해지면 별도 ADR.

## 1. 캡슐

| 항목 | 내용 |
|---|---|
| 목표 | 폰에서 조직·내 프로필·저장소·코드(트리·파일·브랜치)·PR(목록·상세·diff)·이슈(목록·상세·댓글)·Actions(실행·job·로그)를 읽는다. gh에 로그인된 여러 GitHub 계정 중 하나를 골라, 공개 저장소 또는 그 계정 권한이 있는 저장소를 `owner/repo`나 github.com URL로 열어 같은 조회를 한다. |
| 관측 | §2 |
| 가설/반증 | §3 |
| 범위 | §4 |
| 불변식 | §5 |
| 완료조건 | §10 |

## 2. 관측 (2026-10-18 이 Mac Studio에서 직접 실행)

- gh 2.101.0, `/opt/homebrew/bin/gh`. `gh auth status --json hosts`: host는 `github.com` 하나, 계정 2개. `winterholic`(active, keyring, scopes `gist, read:org, repo, workflow`), `daeseon-dev`(비활성, keyring, 같은 scopes). 토큰 값은 읽지 않았다.
- `gh api rate_limit --jq .resources.core`(active 계정): `{"limit":5000,"remaining":4998,...}`. 비활성 계정 `daeseon-dev`의 실호출은 하지 않았다(미검증).
- `gh auth token --help`: `--hostname`, `--user` 지원. `gh auth switch`는 호스트의 전역 active 계정을 바꾼다(공식 매뉴얼). `GH_TOKEN`/`GITHUB_TOKEN`은 저장된 자격보다 우선한다(공식 gh environment).
- GitHub 공식 문서로 확인한 제약(해당 페이지 직접 조회):
  - 기본 한도: 인증 사용자(PAT/OAuth) 시간당 5,000, 비인증 60. 응답 헤더 `x-ratelimit-limit/remaining/used/reset/resource`. 초과 시 403 또는 429. `retry-after`가 있으면 그만큼 기다린다. 보조 한도: 동시 100, 분당 900 포인트. 요청은 직렬 권장. ETag `304`는 올바른 인증이면 기본 한도에 세지 않는다.
  - 페이지네이션: `per_page` 최대 100(기본 30). `Link` 헤더를 따르고, `rel="last"`는 계산 불가면 없다.
  - 저장소 내용: 1MB 이하 전체 지원, 1~100MB는 raw/object만, 100MB 초과 미지원. 디렉터리 1,000개 상한(초과 시 Git Trees). 심링크·서브모듈은 별도 형태. Git Trees `recursive`는 100,000개·7MB 상한, `truncated` 플래그.
  - PR 파일 목록: 최대 3,000개, 페이지당 최대 100. 큰 diff는 오류가 날 수 있다(구체 상태코드는 미확인). `mergeable`은 `null`일 수 있다(백그라운드 계산 중).
  - Issues API는 PR도 반환한다(`pull_request` 키로 구분).
  - Actions job 로그: `GET /repos/{o}/{r}/actions/jobs/{id}/logs`는 302, 링크는 1분 뒤 만료, 비공개 저장소는 `repo` scope 필요.
  - 비공개 자원에 권한이 없으면 403이 아니라 404. `User-Agent` 필수, `X-GitHub-Api-Version` 지정 권장(기본 `2022-11-28`, 지원 종료 2028-03-10).
- 코드 관측(읽기 전용): back `main` 6b60835는 깨끗하다. front `main` 023516f는 다른 작업의 미커밋 변경이 36개 파일에 있다(보존 대상). back·front에 병행 worktree가 각각 20개 안팎이다. ADR 최신은 0041이고 어느 worktree에도 0042는 없다.
- 기존 패턴: FastAPI 라우터는 `api/*.py` + `main.py` 라우터 목록, 인증은 `User` 의존성, 오류 본문은 `{code,message}`(main.py 핸들러), 외부 HTTP는 `httpx.AsyncClient(timeout=…)`(weather), 호스트 허용목록은 `reverse.normalize_target`, 비밀 마스킹은 `oplog/scrub.py`. 프론트는 `react-router` 지연 라우트 + `shell/features.ts` 한 줄로 메뉴 등록, 목록은 `useInfiniteQuery`(OpsLogsPage), 마크다운은 `AppMarkdown`(원시 HTML 미렌더).
- 프론트 `client.ts`는 401에서 refresh를 시도하고 실패하면 로그아웃한다. **GitHub 상류의 401을 그대로 401로 전달하면 앱 로그아웃을 유발한다.**
- PMS: `winterholic-ttakkari`에 GitHub 관련 기존 작업 없음(제목 검색 "GitHub"·"깃" 0건), 최상위 WBS 1~19.

## 3. 가설과 반증 조건

| # | 가설 | 반증/미검증 | 확인 시점 |
|---|---|---|---|
| H1 | `gh auth token --hostname github.com --user <login>`로 전역 active를 바꾸지 않고 계정별 토큰을 얻어, 백엔드가 직접 `api.github.com`을 호출할 수 있다. | 백엔드 LaunchAgent 환경에서 keyring 접근 실패·PATH 부재·프롬프트면 반증. 비밀 읽기를 피하려고 이번 Run에서는 실행하지 않았다(미검증). | GH01 첫 작업(종료코드만 확인, 출력 폐기) |
| H2 | 1인 모바일 탐색은 시간당 5,000 한도 안이다(캐시·ETag 포함). | PR 파일 수백 개·로그 반복 조회로 `remaining` 급감이면 반증. | GH05 한도 측정 |
| H3 | 선택 계정의 `repo`/`read:org` scope로 비공개·조직 저장소가 읽힌다. | 조직 OAuth 앱 제한·SAML SSO면 403과 `X-GitHub-SSO`. `daeseon-dev` 실호출은 미검증. | GH01/GH05 실계정 점검 |
| H4 | Actions 로그 302의 최종 호스트를 고정 접미사 목록으로 제한할 수 있다. | 공식 문서에 호스트 목록이 없다(확인 필요). 실호출로 `Location` 호스트를 관측해 고정. | GH04 |
| H5 | 비인증(60/h) 공개 조회는 불필요하다. | 계정이 하나도 없을 때만 필요. v1은 미지원, `no_account` 안내. | — |

## 4. 범위

- 포함(전부 GET): 계정 목록, 내 프로필, 조직 목록·조직 개요·조직 저장소, 사용자/조직 owner 개요, 저장소 요약·브랜치·README, 디렉터리/파일 내용, PR 목록·상세·파일별 patch, 이슈 목록·상세·댓글, Actions run 목록·상세·job·로그, 입력 해석(`owner/repo`·github.com URL).
- 제외(명시): GitHub 쓰기 전반, GHES·ghe.com 등 github.com 외 호스트, 비인증 모드, 코드 검색·커밋 목록·diff 비교·blame·리뷰 코멘트·체크런·릴리스·Gist·알림, 에이전트/MCP 노출(소유자 UI 전용 — 회사 계정 데이터가 개인·회사 세션으로 섞이지 않게 하는 경계), 이미지 인라인 표시, 서버측 디스크 저장.
- 마이그레이션 없음(테이블 신설 없음). generation protocol 변경 없음.

## 5. 불변식

1. **토큰은 백엔드 프로세스 메모리에서만 존재한다.** 응답·로그·오류·캐시 키·argv·PMS·audit·프론트 저장소에 없다. 토큰 보유 객체는 `repr`을 마스킹한다.
2. **전역 gh 상태를 바꾸지 않는다.** `gh auth switch`·`login`·`refresh`·설정 파일 쓰기 금지. 허용되는 gh 호출은 `gh auth status --json hosts`(메타데이터)와 `gh auth token --hostname github.com --user <검증된 login>` 둘뿐이고, 셸 없이 배열 exec, 환경의 `GH_TOKEN`/`GITHUB_TOKEN`/`GH_ENTERPRISE_TOKEN`은 제거하고 `GH_PROMPT_DISABLED=1`, 타임아웃 5초.
3. **요청별 계정 선택.** 모든 엔드포인트가 `account`(login)를 필수로 받는다. 값은 gh 목록(state=success)에 있는 login만 허용(정규식 + 목록 일치, TTL 60초). 누락은 422(기본 계정을 서버가 추정하지 않는다). 동시 요청은 서로 다른 토큰을 쓰며 공유 상태가 없다.
4. **호스트는 상수.** `https://api.github.com`만 호출한다. 사용자 입력 URL은 절대 fetch하지 않고 고정 템플릿의 경로 조각으로 매핑한다. 리다이렉트 자동 추적 금지. 로그 302만 수동으로 따르되 `Authorization` 없이, `https`·443·허용 접미사 호스트일 때만(H4로 고정, 확인 전에는 로그 기능 비활성).
5. **읽기 전용.** 클라이언트는 `get()`만 노출한다. 허용 경로는 템플릿 목록에 있는 것만이다.
6. **캐시 격리.** 키 = (host, login, 경로 종류, 정규화 경로·쿼리, Accept). 계정 간 공유 없음. 메모리 LRU만 사용하고 항목 500개·합계 32MB 상한. TTL: 목록 30초, 상세 60초, 40자 SHA로 고정된 내용 10분. TTL 후 ETag 재검증(304는 한도 미소모). 계정이 gh 목록에서 사라지면 그 login의 캐시를 비운다.
7. **응답은 화이트리스트 필드로 좁혀 반환**한다(상류 JSON을 통째로 전달하지 않는다). 상류 스키마가 어긋나면 `gh_schema_changed`(내 오류와 구분).
8. 프론트는 **GitHub 본문 HTML을 렌더하지 않고**(AppMarkdown), 외부 링크는 https github.com만 새 탭(`noopener noreferrer`)으로 연다.
9. 상류 401을 401로 전달하지 않는다(앱 세션 로그아웃 방지). `gh_auth_expired`는 502.

## 6. 호출 계층 (dev-api-integration 적용)

계약 표(공식 문서 기준):

| 항목 | 값 |
|---|---|
| 한도 | 5,000/h 기본, 보조 동시 100·900점/분. 클라이언트 스로틀로 한도의 80% 아래 운용(로그인당 동시 4, 전체 8). |
| 타임아웃 | 연결 3s·읽기 10s·전체 15s(로그 다운로드 20s). 예외 없음. |
| 재시도 | GET만, 5xx·전송 오류만 최대 2회(1s·2s, ±50% 지터). 4xx는 재시도하지 않는다. 429·한도 소진 403은 `retry-after`/`x-ratelimit-reset`을 존중하고 재시도하지 않고 `gh_rate_limited`(+reset)로 반환. |
| 응답 상한 | JSON 2MiB(`pulls/files`는 4MiB), 파일 본문 1MiB(초과는 메타만+`too_large`), 로그 다운로드 8MiB(초과는 `gh_too_large`), 로그 창 2,000줄·256KiB. 스트림으로 읽으며 바이트 카운트로 중단. |
| 페이지네이션 | 쿼리는 `page`(1~200)·`per_page`(≤50). 상류 `Link`의 `rel=next` 존재 여부로 `has_more`를 만들고 상류 URL은 전달하지 않는다. 이슈는 PR을 걸러내 페이지당 개수가 줄 수 있으므로 개수가 아닌 `has_more`로 더보기를 판단. |
| 멱등 | 전부 조회. |
| 에러 체계 | 아래 오류 표. 성공 응답 속 오류(200+에러) 없음, 있다면 `gh_schema_changed`. |
| 버전 | `X-GitHub-Api-Version: 2022-11-28` 명시, `User-Agent: ttakkari-github-explorer`, `Accept: application/vnd.github+json`. |
| 서킷브레이커 | v1 미도입(1인 저트래픽, 타임아웃+제한 재시도+한도 게이트로 충분). 도입 트리거: 연속 상류 5xx/타임아웃이 반복돼 UI가 대기하는 사례 발생 시. |
| 토큰 갱신 | 토큰은 메모리에 300초 보관. 상류 401이면 폐기하고 새로 받아 1회만 재시도, 그래도 401이면 `gh_auth_expired`. |

장애 시나리오: 상류 다운/느림 → 15초 내 `gh_timeout`(504)·재시도 2회 후 `gh_upstream_error`(502), 캐시가 있으면 `stale:true`로 제공 / 429·소진 → 한도 오류+reset 카운트다운, 캐시 우선 / 스키마 변경 → `gh_schema_changed` / 계정 만료 → `gh_auth_expired`.

한도 가드: 마지막으로 본 `remaining`이 0이면 reset까지 캐시 없는 요청을 거절, 100 이하이면 응답 `rate.low=true`. `/rate_limit` 조회가 한도에 세지 않는지는 확인 필요이므로 v1은 응답 헤더만 쓴다.

로그 마스킹: 접근 로그는 라우트 종류·상태·소요 시간만 남긴다(경로의 owner/repo는 남기되 쿼리·헤더·본문 제외). 오류 문자열은 `oplog.scrub`을 통과시킨다. Actions 로그 본문은 GitHub이 비밀을 이미 가리지만 추가로 `scrub`을 창 단위로 적용하고 응답에 `masked:true`를 둔다.

## 7. 입력 검증 (SSRF·인젝션·경로)

- owner `^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$`, repo `^[A-Za-z0-9._-]{1,100}$`이면서 `.`·`..`·`.git` 단독 거부, login은 owner 규칙, 번호는 1~2^31 정수, runId·jobId는 양의 정수 문자열.
- 파일 경로는 NUL·제어문자·백슬래시·`..`·빈 세그먼트·선행 `/` 거부, 길이 ≤1024, 세그먼트별 `quote(safe="")`로 인코딩해 템플릿에 끼운다. ref는 ≤255자, 제어문자·`..`·선행 `-`·`~^:?*[\ ` 거부, 인코딩해서 `ref` 쿼리로만 전달.
- `GET /api/github/resolve?q=`: `owner/repo`(.git 허용) 또는 `https://github.com/...`·`https://www.github.com/...`만. 거부: 그 외 스킴, userinfo, 포트, 다른 호스트(`unsupported_host`), IP, 유니코드 점·퍼센트 이중 인코딩, 길이 >2048. 해석 결과는 URL이 아니라 `{kind, owner, repo?, number?, ref?, path?}`이며 프론트 경로는 이 필드로 만든다. 쿼리·프래그먼트는 버린다. `tree|blob/<ref>/<path>`의 ref에 슬래시가 있으면 모호하므로 v1은 첫 세그먼트를 ref로 보고 404이면 최대 3번까지 더 긴 ref로 재시도(GH03).
- 쿼리 파라미터는 엔드포인트별 화이트리스트(state, labels, branch, event, filter 등)와 값 enum·길이 제한을 둔다.
- 필수 부정 테스트: `../`, `%2e%2e`, `%00`, `//`, `@evil.com`, `github.com.evil.com`, `http://127.0.0.1`, `file:`, `git@github.com:o/r`, 긴 입력, 이중 인코딩.

## 8. API 계약 (`/api/github`, 소유자 인증 `User`, 에이전트 라우터 없음)

공통 쿼리 `account=<login>` 필수. 공통 응답 봉투: `{data, page:{page,has_more}?, rate:{limit,remaining,reset,low}, fetched_at, cached, stale?, account}`.

| 메서드·경로 | 용도 | 상류 |
|---|---|---|
| GET `/accounts` | 계정 목록 `{login, host, active, scopes[], state}`, 마지막 관측 rate. account 불필요 | `gh auth status --json hosts` |
| GET `/resolve?q=` | 입력 해석(account 불필요) | 없음 |
| GET `/me`, `/me/orgs`, `/me/repos?sort&page` | 내 프로필·조직·저장소 | `/user`, `/user/orgs`, `/user/repos` |
| GET `/owners/{login}`, `/owners/{login}/repos` | 사용자·조직 개요(type으로 구분)와 저장소 | `/users/{login}`, `/orgs|users/{login}/repos` |
| GET `/repos/{o}/{r}` | 저장소 요약(기본 브랜치·visibility·archived·권한) | `/repos/{o}/{r}` |
| GET `/repos/{o}/{r}/branches` | 브랜치(보호·기본 표시) | `/branches` |
| GET `/repos/{o}/{r}/contents?ref&path` | `kind: dir|file|symlink|submodule`. dir은 항목 정렬(폴더 먼저), file은 `{name,size,sha,binary,truncated,too_large,text?}` | `/contents/{path}` |
| GET `/repos/{o}/{r}/readme?ref` | README 텍스트 | `/readme` |
| GET `/repos/{o}/{r}/pulls?state&page` | PR 목록 | `/pulls` |
| GET `/repos/{o}/{r}/pulls/{n}` | PR 상세(본문·head/base·통계·`mergeable` null은 `computing`) | `/pulls/{n}` |
| GET `/repos/{o}/{r}/pulls/{n}/files?page` | 파일별 `{filename, previous_filename, status, additions, deletions, patch?, patch_missing}` | `/pulls/{n}/files` |
| GET `/repos/{o}/{r}/issues?state&labels&page` | 이슈 목록(PR 제외) | `/issues` |
| GET `/repos/{o}/{r}/issues/{n}` | 이슈 상세, PR 번호면 `kind:"pull"` 안내 | `/issues/{n}` |
| GET `/repos/{o}/{r}/issues/{n}/comments?page` | 댓글 | `/issues/{n}/comments` |
| GET `/repos/{o}/{r}/actions/runs?status&branch&event&page` | run 목록 | `/actions/runs` |
| GET `/repos/{o}/{r}/actions/runs/{id}`, `/jobs?page` | run 상세, job·step | `/actions/runs/{id}`, `/jobs?filter=latest` |
| GET `/repos/{o}/{r}/actions/jobs/{id}/logs?from_line&lines` | 로그 창 `{lines[], total_lines, from_line, masked, truncated}` | `/actions/jobs/{id}/logs` |

오류(`{code,message,reset?,retry_after?,reason?}`, 앱 오류 형식 유지):

| code | HTTP | 뜻·UI 상태 |
|---|---|---|
| `no_account` | 503 | gh 계정 없음 → Mac Studio에서 `gh auth login` 안내 |
| `account_unavailable` | 404 | 목록에 없는 계정/만료 상태 → 계정 선택 |
| `invalid_target` / `unsupported_host` | 422 | 입력 오류 |
| `gh_not_found` | 404 | 없거나 이 계정에 권한 없음(비공개는 404) → "권한 부족 또는 없음" + 계정 전환 제안 |
| `gh_forbidden` (`reason`: `sso_required`·`scope_missing`·`policy`) | 403 | 권한 부족. `X-GitHub-SSO` 있으면 `sso_required`, `X-OAuth-Scopes`/`X-Accepted-OAuth-Scopes` 비교로 `scope_missing` |
| `gh_auth_expired` | 502 | 계정 토큰 만료 → Mac Studio에서 재로그인 |
| `gh_rate_limited` | 429 + `Retry-After` | 한도 → reset 카운트다운 |
| `gh_gone` / `gh_unavailable_legal` | 410 / 451 | 이슈 비활성·법적 차단 |
| `gh_unprocessable` | 422 | 상류 거부(예: 큰 diff) → GitHub에서 열기 |
| `gh_too_large` | 413 | 상한 초과 |
| `gh_timeout` / `gh_upstream_error` / `gh_schema_changed` | 504 / 502 / 502 | 오류·재시도 |

## 9. 프론트 설계 (frontend-design 적용)

브리프: 주제 = 폰에서 GitHub 읽기. 사용자 = 소유자 1인(엄지 한 손, 이동 중). 1차 과업 = PR·이슈·Actions 결과를 빠르게 읽고 되돌아오기. 기본값 검문 결과: 데스크톱 GitHub 축소판이 아니라 "목록→상세→뒤로" 한 스택, 대상 입력과 계정 전환은 항상 상단에서 닿는 위치, 한 화면 한 과업.

- 라우트(`/github/*` 지연 청크, `shell/features.ts` 한 줄, `linkTarget.ts`의 `APP_ROUTE_ROOTS`에 `github` 추가 — 테스트가 App.tsx와 대조):
  `/github`(홈: 계정·대상 입력·내 저장소/조직/최근), `/github/:owner`(사용자·조직), `/github/:owner/:repo`(개요: 코드·PR·이슈·Actions 탭), `…/code?ref=&path=`(트리·파일 한 라우트), `…/pulls`, `…/pulls/:n`(`?tab=files`), `…/issues`, `…/issues/:n`, `…/actions`, `…/actions/runs/:id`, `…/actions/runs/:id/jobs/:jobId`. 전부 `?as=<login>`을 유지한다.
- URL 공유·새로고침: 상태는 전부 경로+쿼리(`as`, `state`, `page`, `ref`, `path`, `tab`, `from_line`)에 둔다. `ref`·`path`를 쿼리로 둔 이유는 브랜치명의 `/` 때문에 경로 파싱이 모호해지기 때문이다. `as`가 없거나 목록에 없으면 계정 선택 화면으로 보내 `replace`로 채운다(마지막 선택은 `localStorage`에 login만 저장). 최근 대상도 `owner/repo`+login만 저장한다.
- 뒤로가기: 상세는 push, 목록 필터·페이지는 searchParams, 목록은 React Query 캐시로 복귀 시 즉시 표시, 스크롤은 라우터 복원을 쓴다.
- 상태 전수: 로딩(스켈레톤)·빈·권한부족(`gh_not_found`/`gh_forbidden` 문구 분리, 계정 전환 버튼)·만료(`gh_auth_expired`, Mac Studio 안내)·한도(reset 카운트다운, 캐시 표시)·오류(재시도)·부분(truncated/too_large/patch_missing, GitHub에서 열기 링크). `StateView` 하나를 GH02에서 만들고 전 화면이 쓴다.
- 긴 내용:
  - 파일명·경로: basename 전체 표시(`overflow-wrap:anywhere`), 디렉터리는 한 줄 말줄임 + 탭하면 전체, `title`·`aria-label`로 전체 제공.
  - 코드: `<pre>` + 줄번호 거터(sticky), 가로 스크롤 기본·줄바꿈 토글, `tab-size`, 리가처 끔, 400줄 단위로 "다음 줄" 렌더. Monaco는 쓰지 않는다(모바일 용량).
  - diff: 파일 단위 접기(기본 접힘, 변경 요약 줄), +/− 기호와 색 동시 사용(색맹 대응), hunk 단위 렌더, patch 200KB 초과는 접힌 채 "펼치기" 확인, 가로 스크롤.
  - 로그: 2,000줄 창 + 이전/다음, 줄 단위 `white-space:pre`, 실패 step 먼저 표시.
  - 마크다운(README·이슈·댓글): `AppMarkdown`, 이미지는 링크로만, 원시 HTML 미렌더.
- 터치 목표 44 이상, 하단 탭/safe-area 준수. 새 색·px·hex·`vh` 리터럴 금지(`scripts/ui-lint.mjs` 기준선에 걸림), 기존 `tk-*` 토큰, 높이는 `dvh`.
- 아이콘: lucide 1.x는 브랜드 아이콘이 없으므로 `Icon.tsx`에 `git-branch`·`git-pull-request` 등 일반 아이콘을 추가한다.

## 10. 파일 소유권·구현 순서·검증

구현은 전용 worktree에서 한다(`back-github`, `front-github`, 브랜치 `feat/github-explorer-20261018`, origin/main 기준). 현재 front main의 미커밋 변경 36개와 타 worktree는 건드리지 않는다.

backend 소유권(`back/src/ttakkari/github/`):

| 파일 | 소유 작업 | 내용 |
|---|---|---|
| `targets.py`, `tests/test_github_targets.py` | GH01 | 검증기·resolve(순수 함수) |
| `accounts.py` | GH01 | gh 목록·토큰 획득·메모리 보관·`Secret` |
| `client.py`, `cache.py` | GH01 | httpx 래퍼(타임아웃·상한·재시도·ETag·한도·오류 매핑) |
| `schemas.py` | GH01(공통)·각 트랙(자기 모델 추가) | 화이트리스트 응답 모델 |
| `routes_identity.py` | GH01 | accounts·resolve·me·owners·repos |
| `routes_code.py` | GH03 | repo·branches·contents·readme |
| `routes_pulls.py`, `routes_issues.py` | GH04 | PR·이슈 |
| `routes_actions.py`, `logs.py` | GH04 | Actions·로그 창 |
| `api/github.py`, `main.py` 2줄 | GH01만 | 라우터 집합·등록. 다른 트랙은 자기 `routes_*.py`만 추가하고 집합 파일의 include 줄은 GH01이 미리 둔 자리를 사용 |

frontend 소유권:

| 경로 | 소유 작업 |
|---|---|
| `api/githubTypes.ts`, `api/githubClient.ts`(`client.ts` 비수정, `json()` 재사용 필요 시 export 1줄만 GH02가 추가) | GH02 |
| `pages/GithubPage.tsx`(중첩 `Routes`), `App.tsx` 2줄, `shell/features.ts` 1줄, `linkTarget.ts` 1줄, `ui/Icon.tsx` | GH02 |
| `features/github/state/*`(StateView·계정 선택·대상 입력·최근), `github.css` | GH02 |
| `features/github/home/*`(내 프로필·조직·저장소 목록) | GH02 |
| `features/github/code/*`, `github-code.css` | GH03 |
| `features/github/pulls/*`, `features/github/issues/*`, css | GH04 |
| `features/github/actions/*`, css | GH04 |

순서: **GH00(설계) → GH01(백엔드 코어, 차단) → [GH02 프론트 셸·홈 ∥ GH03 코드 백엔드] → GH03 코드 프론트 ∥ GH04 PR/이슈/Actions(백엔드·프론트 각 독립) → GH05 통합·보안·모바일 검증 → GH06 배포.** GH01이 `back/tests/fixtures/github/*.json`(계약 예시)을 먼저 내놓으면 프론트 트랙은 백엔드 없이 fixture로 병행할 수 있다.

검증(완료조건):
- backend: `uv run pytest tests/test_github_*.py`, `uv run ruff check src tests`, 무타임아웃 grep(`grep -rn "httpx\.\|AsyncClient(" src/ttakkari/github | grep -v timeout` 0건), 금지 패턴 grep(`gh auth switch|shell=True|follow_redirects=True` 0건), 토큰 누출 테스트(가짜 토큰 문자열이 응답·caplog·오류 어디에도 없음), 동시 두 계정 요청에서 `MockTransport`가 기록한 Authorization이 각 요청의 계정 토큰과 일치, 캐시 계정 격리 테스트, SSRF 부정 표(§7), 오류 표 전수 단위 테스트.
- frontend: `pnpm vitest run`, `pnpm build`(tsc -b), `pnpm lint`(eslint+ui-lint 기준선 증가 0), `pnpm check:layout` + WebKit 스크린샷 360·390 폭에서 긴 파일명·긴 코드 줄·큰 diff·로그 창.
- 실계정(별도 표기): 두 계정 각각 `/me`·공개 저장소·비공개 저장소 조회, 응답 `rate.remaining` 차이 기록, 계정 A/B 동시 조회에서 서로의 비공개 저장소가 섞이지 않음, Actions 로그 `Location` 호스트 관측 후 허용목록 고정.
- 실기기/브라우저(별도 표기): iPhone Safari PWA에서 목록→상세→뒤로, 새로고침 후 URL 복원, URL 공유 열기.
- 배포: backend는 검증 통과 후 커밋·푸시하고 전체 SHA로 `request_backend_deploy` 예약(마이그레이션·protocol 변경 없음), 프론트는 푸시 후 Vercel 커밋 상태 success 확인 + 실기기 점검. 예약 접수는 배포 성공이 아니다.

## 11. 미검증·확인 필요

- H1(계정별 토큰 획득이 백엔드 실행 환경에서 되는지)·H3 `daeseon-dev`·SSO 실동작, H4 로그 `Location` 호스트.
- 큰 PR diff의 정확한 상류 상태코드, `/rate_limit`이 한도에 세지는지.
- gh가 keyring 접근에 macOS 프롬프트를 띄우는지(LaunchAgent 환경).
- 모바일 실기기 렌더링·성능(코드 400줄 렌더, 큰 diff).
