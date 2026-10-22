# 제작 기록 API (프론트 '제작 기록' 화면용)

범위: AI Creative Studio 3·4단계가 추가한 `/api/creative/...` 경로. 기존 G0 경로(`/productions`, `/artifacts`, `/attempts`)는 그대로다.
모든 경로는 소유자 인증(`require_user`)이 필요하다. 예외는 토큰 서명 미리보기 `GET /productions/{pid}/artifacts/{sha}`뿐이다.
구현: `src/ttakkari/api/creative.py`. 시험: `unit_tests/test_creative_production_api.py`. 설계: `docs/adr/0068-creative-engine-adapters-and-director.md`.

## 읽는 법: 한 번의 GET으로 화면 전체

`GET /api/creative/productions/{pid}/record` — 저장된 상태와 sidecar에서 조립한 읽기 모델이다. 새 사실을 만들지 않는다.

```jsonc
{
  "production_id": "…", "brief": "…", "cancelled": false,
  "manifest": {"revision": 12, "content_hash": "…", "policy_ref": "research-null-approval"},
  "fps": {"num": 24, "den": 1}, "duration_frames": 72, "format": [640, 360],
  "bible": {"revision": 2, "palette": [...], "typography": {...}, "composition": {..., "color": {"lut": {"applied": false}}},
            "camera": {...}, "negative_rules": [], "author": "creative-director", "approval_event_id": null},
  "timeline": {"revision": 3, "sections": [...], "beats": 5, "cues": 5},
  "shots": [{
    "shot_id": "shot-1", "start_f": 0, "end_f": 24, "revision": 3,
    "intent": "제목 카드", "engine": "remotion", "profile": "title-card",
    "params": {"title": "…"}, "selection": "auto|explicit-lab|fixed", "selection_reason": "…", "seed": 1,
    "acceptance_criteria": ["allow_freeze:0-24:이유"],
    "stale": false, "transition_neighbor_stale": true,
    "current_attempt": {"attempt_id": "…", "state": "queued|running|collecting|review_pending|failed|cancelled",
                        "error_kind": null, "error_details": [],
                        "output": {"sha256": "…", "bytes": 12345, "media_type": "video/mp4"},
                        "receipt": {"engine": "…", "execution_mode": "real|mock", "real_execution": true, "wall_seconds": 8.5,
                                    "metrics": {"rss_peak_mb": 2006}, "media": {"duration": 1.0, "video": {...}}}},
    "review": {"review_id": "…", "verdict": "pass|fail|review_pending", "criteria": {"decode": "pass", "black_frames": "fail", ...},
               "reviewer": "machine-check/v1", "independent": false, "approval_event_id": null, "scope_note": "…", "dropped_candidates": 0},
    "keyframes": [{"frame": 0, "timeline_frame": 0, "reasons": ["shot_boundary"], "sha256": "…", "luma": 12.3, "motion": 0.0}],
    "revisions": [{"revision": 1, "content_hash": "…", "engine": "…", "profile": "…", "created_at": "…"}],
    "attempts": [{"attempt_id": "…", "shot_revision": 2, "state": "review_pending", "error_kind": null, "created_at": "…"}],
    "feedback": [{"feedback_id": "…", "from_revision": 2, "text": "…", "decided_by": "user-overrides|director", "reason": "…", "changes": {...}, "at": "…"}]
  }],
  "final": {"composition_key": "final:…", "output_sha256": "…", "execution_mode": "real", "real_execution": true,
            "matches_current_shots": true, "review": {"verdict": "pass", "criteria": {...}, "approval": null, "scope_note": "…"}} ,
  "stale": {"shots": [], "transition_neighbors": [], "bible_assets": [], "reviews": ["bible"]},
  "direction": [{"step": "bible|timeline|shots|shots-selection|direct", "status": "ok|failed", "repairs": 0, "at": "…"}],
  "events_tail": [...],
  "engines": [{"engine_id": "ffmpeg", "execution_mode": "real", "auto_eligible": true, "installation": "available", "verification": "lab_executed"}]
}
```

화면에 쓸 때 지킬 점:
- `review.verdict`가 `pass`여도 **기계 검수**일 뿐이다. `scope_note`를 함께 보이고 `independent=false`, `approval_event_id=null`임을 숨기지 않는다. 사람 승인 버튼을 만들어도 이 필드를 채우는 경로는 아직 없다.
- `execution_mode="mock"` 또는 `real_execution=false`인 attempt는 실제 생성 성공이 아니다. 계약 시험 흔적이라고 표시한다.
- `stale=true`인 샷의 `current_attempt`는 이전 입력의 출력일 수 있다. `final.matches_current_shots=false`이면 최종 영상이 현재 샷들과 다르다.
- `current_attempt.state`가 `queued|running|collecting`이면 폴링한다(2~3초). 진행은 `events_tail`과 attempt 상태로 읽는다.
- 생성형 프로필(`comfy`)은 프레임 수·해상도를 보장하지 않아 `review.criteria`의 `frame_count|resolution|fps|duration`이 `unknown`일 수 있다. 최종 조립이 컨폼한다.

## 미리보기 자산

1. `POST /productions/{pid}/artifacts/{sha}/link` → `{url, expires_at}` (소유자 인증). sha는 `record`의 `output.sha256`, `keyframes[].sha256` 등이다.
2. `GET {url}` — 서명 토큰 쿼리가 있는 URL. 토큰은 제작 ID·sha에 묶이고 만료된다. 만료·위조는 403, 모르는 sha는 404.

현재 `FileResponse`라 HTTP Range를 지원한다. G0 CAS 자산 목록의 출력 `media_type`은 `application/octet-stream`이다(G0 기존 동작). 그래서 `record`의 `output.media_type`은 engine receipt가 적은 종류(`video/mp4`)를 쓰지만, 미리보기 URL의 응답 `Content-Type`은 `application/octet-stream`이다. 영상 재생은 `<video src>`에 URL을 직접 주고 `record`의 종류로 판단한다. 키프레임 PNG는 `image/png`로 응답한다.

## 쓰는 법

| 용도 | 메서드·경로 | 본문 | 응답 |
|---|---|---|---|
| 엔진 목록 | `GET /engines` | - | 엔진별 설치·검증·정책 세 축과 `auto_eligible`, 프로필 매개변수 스키마 |
| 엔진 측정 | `POST /engines/{id}/measure` | - | probe 결과(렌더 없음) |
| 엔진 smoke | `POST /engines/{id}/smoke` | - | 짧은 실제 렌더 증거(수 초~수십 초 걸림) |
| 엔진 정책 | `POST /engines/{id}/policy` | `{policy, reason}` | 실측·smoke 증거가 없으면 `auto_eligible`로 못 올린다(422) |
| 감독 시작 | `POST /productions/{pid}/direct` (202) | `{brief?, audio_sha256?, speech_lines?, voice?, duration_seconds?, fps?, size?}` | `{status:"started"}`. 진행·실패는 `record.direction` |
| 샷 렌더 | `POST /productions/{pid}/shots/{shot_id}/render` (202) | `{retry?}` | queued attempt. 끝나면 자동 검수 |
| **장면 피드백 재제작** | `POST /productions/{pid}/shots/{shot_id}/feedback` (202) | 아래 | 새 revision·attempt |
| 검수 다시 | `POST /productions/{pid}/attempts/{aid}/review` | - | 이미 있으면 `reused=true` |
| 재개 | `POST /productions/{pid}/resume` (202) | - | 끝나지 않은 attempt만 이어서 실행 |
| 최종 조립 | `POST /productions/{pid}/final` (202) | `{audio_sha256?, audio_gain_db?}` | stale·결함 샷이 있으면 즉시 422 |
| 취소 | `POST /productions/{pid}/attempts/{aid}/cancel` | - | G0 기존. 제작 전체가 취소되고 엔진은 협조적으로 중단 |

### 장면 피드백 재제작

```http
POST /api/creative/productions/{pid}/shots/{shot_id}/feedback
{
  "expected_shot_revision": 3,
  "text": "끝에서 화면이 검게 꺼져요. 페이드 없이.",
  "idempotency_key": "ui-<uuid>",
  "overrides": {"engine": null, "profile": null, "params": {"fade_out_frames": 0}, "drop_params": [], "seed": null, "intent": null}
}
```

- `overrides`를 주면 그 값이 그대로 적용된다(`selection="explicit-lab"`). 생략하면 창작 감독(LLM)이 `text`를 보고 변경을 제안한다(`selection="auto"`, 자동 선택 후보 안에서만).
- 어느 경우든 **그 샷의 새 revision 하나와 새 attempt 하나**만 생긴다. 다른 샷의 revision·출력 hash, 타임라인, 바이블은 바뀌지 않는다. 이전 revision의 출력·검수는 이력에 남는다.
- 응답 `202`: `{feedback_id, shot_id, from_revision, to_revision, attempt_id, state:"queued", decided_by, reason, changes}`. 이후 `record`를 폴링한다.
- 같은 `idempotency_key`와 `expected_shot_revision`으로 다시 보내면 새 revision 없이 같은 결과를 돌려준다.
- 오류: `409` 샷이 이미 바뀜(`expected_shot_revision` 불일치) 또는 취소된 제작, `422` 매개변수 검증 실패·알 수 없는 엔진/프로필·비어 있는 텍스트·감독이 후보 밖 엔진을 고름, `404` 제작·샷 없음.
- 피드백 원문, 결정 주체, 이유는 `record.shots[].feedback`에 불변으로 남는다.
- 재제작이 끝나도 `final`은 자동으로 다시 만들어지지 않는다. `final.matches_current_shots=false`가 되며 `POST …/final`로 다시 조립한다.

### 오류 코드 요약

`404` 대상 없음, `409` revision 충돌·취소된 제작, `422` 검증 실패(어댑터 매개변수 스키마, 엔진 미등록, stale·결함 샷, 후보 밖 엔진), `403` 만료·위조 토큰. 서버 내부의 엔진 실패는 HTTP 오류가 아니라 attempt의 `failed`/`error_kind`와 `error_details`로 남는다.
`error_kind`: `empty_output`(빈 결과), `collect_failed`(수집·엔진 실패, `error_details`에 `[engine_error:<종류>]`), `output_integrity_failed|missing_output|incomplete_collection`(G0 수집 검증).

## 미구현·미검증 (화면이 가정하면 안 되는 것)

- 사용자 승인(approval) 경로: 없다. 모든 승인 필드는 `null`이다.
- 독립 검수(`independent=true`)와 VLM 의미 검수: 없다. 기계 검수만 있다.
- 실제 Comfy 생성: 이 API로 시작하는 경로는 있으나(`engine=comfy`, 명시 선택만) 실제 생성은 실행·검증하지 않았다.
- 실제 감독 LLM 경로(`SessionRunDirectorLlm`)는 코드만 있고 이 환경에서 실행하지 않았다(테스트 DB 불가).

## 부록: managed Comfy I2V 입력 (`/api/comfy/jobs`)

E1 준비용 확장이다. `profile_id=wan22-5b-i2v-draft`는 **준비됨·미검증**(`GET /api/comfy/profiles`의 `verification="prepared-unverified"`)이다.

```http
POST /api/comfy/jobs
{"profile_id": "wan22-5b-i2v-draft", "prompt": "…", "seed": 2101,
 "image": {"production_id": "<uuid>", "asset_id": "keyframe_flux_cafe.png", "sha256": "<64 hex>"}}
```
- `image`는 이 제작 manifest에 있는 자산의 이름과 sha256 쌍이다. 먼저 `POST /api/creative/productions/{pid}/artifacts`로 올린다. 경로·URL·그래프 필드는 422다.
- I2V profile은 `image`가 없으면, T2V profile은 `image`가 있으면 lease를 잡기 전에 422다. 기존 T2V 요청(`image` 없음)은 그대로 동작한다.
- 오류: 404 제작 없음, 422 id·sha 불일치(다른 제작 자산 포함)·이미지 검증 실패, 409 공용 lease 사용 중.
- 작업 응답에 `input_image`(해시·크기·업로드 이름)와, 업로드 단계 결과 `input_upload`가 보인다. 업로드가 실패하면 `status=failed`, `error="input_upload_failed: …"`, `input_upload.prompt_submitted=false`이며 GPU 제출은 없었다.
- creative 샷에서 쓰려면 `ProductionPipeline.apply_shot_plan`의 샷 계획에 `assets=[sha]`를 준다(코드 경로). 엔진 `comfy`, profile은 위 id, 선택은 `explicit-lab`만 허용된다. 감독 LLM 응답 스키마와 피드백 API에는 자산 필드가 아직 없다(미구현).
