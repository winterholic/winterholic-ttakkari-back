# 제작 기록(Production Records)

스튜디오에서 "어떤 과정으로 만들어 어떤 결과가 나왔는지"를 목록과 상세로 보여 주는 읽기 전용 모델이다. 코드는 `src/ttakkari/creative/records.py`, 라우트는 `src/ttakkari/api/creative.py`.

## 원천 세 가지

| 출처 | id | 원천 | 비고 |
|---|---|---|---|
| g0 | `g0-<uuid>` | G0 제작 제어부 상태 파일(`creative-studio/productions/<id>/state.json`) | 시도 receipt·검수 영수증을 단계·샷·검수로 정규화한다 |
| v1 | `v1-<uuid>` | v1 creative 프로젝트·렌더(`creative-studio/<id>/project.json`) | 장면은 샷, 렌더 시도는 수정·재렌더로 본다. 검수 기록은 없다 |
| lab | `lab-<slug>` | `<lab 루트>/<영역>/records/<slug>/record.json` | 실험 매니페스트. `slug`는 폴더 이름과 같아야 한다 |

lab 루트는 설정 `TTAKKARI_STUDIO_LAB_ROOT`(기본 `~/development/projects/ttakkari-studio-lab`)다. 매니페스트는 1MB 이하, 루트 안에 있어야 하며 깨진 파일은 목록에서 빠지고 `lab.skipped`에 사유가 남는다.

## 스키마(`schema_version: 1`)

`RecordBody`(매니페스트와 상세 응답이 공유). 값이 없으면 `null`로 두며 UI는 '미측정'으로 표시한다. 근거 없는 값을 0이나 추정치로 채우지 않는다.

- 식별: `slug`, `title`, `kind`, `summary`, `status`(completed/partial/failed/in_progress/draft), `created_at`, `updated_at`
- 과정: `brief`, `style`, `stages[]`(key·label·status·summary·seconds·details), `revisions[]`(결함 id·증상·조치·fixed/open)
- 샷: `shots[]`(engine·model·prompt·seed·seconds·status·keyframe/output 미디어 id·params)
- 도구·실측: `tools[]`, `metrics[]`(value가 null이면 미측정), `tables[]`, `cost`(`usd`, `paid_services`)
- 결과물: `media[]`(`id`, `kind`=video/audio/image/document, `role`=result/before/after/compare/keyframe/contact_sheet/audio/evidence/reference/document, `path`)
- 검수: `review`(verdict=pass/conditional/fail/unreviewed, `independent`, `criteria[]`, `limitations[]`)

서버가 채우는 필드(`available`, `url`)는 매니페스트 값을 무시하고 덮어쓴다.

## API

- `GET /api/creative/records` — 목록. `source`, `kind`, `engine`, `q`, `limit` 필터. 응답에 `counts`, `kinds`, `engines`, `lab.skipped` 포함
- `GET /api/creative/records/{rid}` — 상세. 미디어마다 5분짜리 서명 `url`이 들어 있다
- `GET /api/creative/records/{rid}/media/{media_id}?token=` — 파일. Range 지원, 토큰이 기록·미디어 id에 묶인다

목록·상세는 기존 creative API와 같은 소유자 인증(`owner-plain`)이고, 미디어는 다운로드 토큰으로 보호되는 `public` 분류다(`/projects/{pid}/media/...`와 같은 방식).

## 미디어 경로 격리

lab 매니페스트의 `path`는 lab 루트 기준 상대경로다. 다음은 모두 거부하고 `available=false`로 표시한다.

- 절대경로, `..`, 역슬래시, NUL 문자
- 심볼릭 링크를 끝까지 해석한 결과가 루트 밖인 경우(파일·디렉터리 링크 모두)
- 선언한 `kind`와 맞지 않거나 허용 목록 밖인 확장자(mp4/webm/mov, wav/mp3/m4a/flac/ogg, png/jpg/webp, md/txt/json/jsonl/lrc/csv/tsv/log). HTML·SVG·스크립트는 서빙하지 않는다
- 매니페스트 파일 자체가 링크로 루트 밖을 가리키는 경우(목록에서 제외)

g0는 `artifacts/<sha256>` 자체 CAS 경로만, v1은 프로젝트 폴더 안만 서빙한다.
