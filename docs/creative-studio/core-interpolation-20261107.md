# Core 후속 — 명시적 B 보간 선택 전달 (2026-11-07)

이번 보간 연결 범위는 완료했다. 전체 ENG/B는 부분이다. 실제 Studio dispatch의 8초 영상은 생성됐으나 **무음 마스터 때문에 최종 machine QA는 fail**이다. semantic/viewing은 unknown, 청취는 미실행이다. 보간 전달을 콘텐츠 품질 합격으로 확대하지 않는다.

## 1. 기준과 보존

HEAD `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`. [Stage2](core-stage2-20261107.md), [Stage3](core-stage3-20261107.md), [B 후속 독립 검증](verification-b-20261107.md)의 긴 관절 포즈 보간 절을 먼저 읽었다. Stage3를 중단하거나 재제작하지 않았다. 과거 Run outbox는 읽지 않았다.

착수 SHA 35파일과 git status를 현재 Run outbox `interpolation/start-source.json`에 저장했다. 대조 결과 기존 소스 변경은 planner.py·dispatch.py·frame_animation.py 3개뿐이다. B의 `FrameAnimationAdapter` 바깥 AST는 동일하다. B 수치 보간·preset·benchmark·검증자 문서, Stage3 lifecycle/consumer, A/C·quality·frontend 및 다른 담당자의 dirty 변경을 보존했다. commit/push/배포 없음.

## 2. 승인 계획에서 renderer까지의 계약

기존 plans create/edit의 `director.scenes[]`와 Stage3 storyboard 장면 스키마에 `interpolation_mode: "hold" | "linear" | null`을 허용한다. 생략/null은 명시적 선택 없음이며 기존 hold를 유지한다. 값이 있으면 strategy는 `frame_animation_2d`여야 한다. 다른 전략에서 선택하거나 미지원 문자열·숫자·bool을 넣으면 검증 오류다. 별도 API/큐/기본 linear 전환은 없다.

```json
{
  "scene_id": "walk",
  "strategy": "frame_animation_2d",
  "preset": "walk_cycle",
  "interpolation_mode": "linear",
  "rationale": "키포즈 사이 수치 선형 보간 선택",
  "shots": [{"shot_id":"walk-shot","action":"지원 걷기 프리셋 사용","layers":["visual"]}]
}
```

`apply_direction`은 명시적 선택만 계획 scene에 투영한다. 보간 변경은 기존 scene 기반 segment input hash와 승인 snapshot에 포함된다. edit 후 revision이 오르고 승인이 닫히며, 다른 장면의 segment hash는 보존된다. 기존 linear를 생략으로 바꾸면 남은 linear 필드를 제거해 hold로 돌아간다. 원본 자산·이전 성공 출력은 삭제하지 않는다.

기존 단일 walk_cycle 장면 구간에서 `_render_producer`가 선택을 실행 `input.json.interpolation_mode`로 보낸다. 실제 `FrameAnimationAdapter.render_segment`는 생략 시 hold, 명시적 hold/linear만 수용하여 `AnimationProjectSpec`에 넣는다. 직접 adapter 호출에 잘못된 값/null을 넣으면 renderer 실행 전 ValueError다. 계획의 null은 투영하지 않으므로 adapter null과 구분된다. linear 이유 문구도 hold 선택이라고 잘못 기록하지 않도록 함께 전달했다.

StoryboardScene은 SceneDirection을 상속하고 기존 materialize가 그 필드를 필터링해 복사하므로, 모델 출력 → 검토 → ProductionDirection 경로에도 자동으로 연결된다. 이를 fixture로 검증했으며 **이번 작업에서 새 LLM 추론은 하지 않았다**. Stage3의 실추론 증거는 별개로 보존한다. UI는 기존 director 입력 필드에 선택을 넣고 기존 approve/production/render를 사용하면 된다. frontend 구현·운영 HTTP 검증은 이번 범위에서 하지 않았다.

## 3. 좁은 반례와 회귀

환경: Codex worker sandbox, 저장소 로컬 PlanStore/ProductionStore/Studio. 공통 환경변수 `PYTHONPATH=src:.:unit_tests UV_CACHE_DIR=/private/tmp/codex-uv-cache`.

```text
uv run pytest unit_tests/test_creative_v2_interpolation_bridge.py -q
RED: 8 failed, 1 passed in 0.57s → 구현 후 9 passed in 0.38s
추가 hold 동등성·storyboard 투영 포함: 11 passed in 0.35s

uv run pytest unit_tests/test_creative_v2_interpolation_bridge.py unit_tests/test_creative_v2_stage3.py -q
30 passed in 3.30s

uv run pytest unit_tests/test_creative_v2_core_integration.py -q -k 'reviewed_direction or director_edit or directed_shot'
3 passed, 15 deselected in 0.52s
```

전용 11건: 승인 계획→실제 ModuleAdapter→FrameAnimationAdapter→ProjectSpec의 생략/hold/linear 3건, 변경 후 재승인·stale revision 거부·미변경 구간 보존·생략 복귀 1건, 미지원 값 4건, 다른 전략 거부 1건, 생략과 hold의 전체 ProjectSpec 동등성 1건, storyboard materialize 보존 1건. 이 계약 검사에서 renderer 캡처 파일은 가짜이며 실영상 증거로 세지 않는다.

`ruff check`는 planner.py·dispatch.py·신규 전용 test·증거 script에서 All checks passed. 신규 test/script format check 통과. adapter 전체의 기존 lint 8건(I001, F401×3, BLE001×2, RUF046×2)은 착수 복사본과 코드·메시지 순서가 같고 **신규 위반 0건**이다. 타 담당 범위를 넓혀 고치지 않았다. `git diff --check` exit 0. 기존 프로젝트 포맷을 일괄 변경하지 않았다. mypy는 이 좁은 계약 작업에서 실행하지 않았으며 타입 검사 통과를 주장하지 않는다.

기존 유효 B 9건·전체 benchmark·Stage2 실제 부분 수정 렌더는 반복하지 않았다. Stage3 19건은 변경된 공통 SceneDirection/상속 스키마의 회귀로만 실행했다.

## 4. 실제 단일 Studio dispatch

실행 script: [studio_dispatch.py](core-interpolation-evidence-20261107/studio_dispatch.py). 실행 시 원문은 현재 outbox `executed-studio-dispatch.py`에 별도 보존했다. 이후 script에는 `--collect-only`와 QA 실패 결과 수집 경로를 추가하고 가독성/린트를 정리했다. 재렌더하지 않았다.

```text
PYTHONPATH=src:. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run python docs/creative-studio/core-interpolation-evidence-20261107/studio_dispatch.py <현재Run outbox>/interpolation/actual
producer segment_succeeded → final qa_failed → script의 성공 assert 실패(exit 1)
원인: 명시적으로 넣은 silent-master.wav, audible max=-91dB ≤ -60dB

같은 script <동일 actual 경로> --collect-only
exit 0: selection=linear, project_selection=linear, frames=192, state=qa_failed
```

실험은 `user_structured`의 8초 단일 walk_cycle이며 하드코딩 응답을 실제 추론으로 표시하지 않았다. 초기 미승인 계획 생성→명시적 approve revision 1→master 자산 등록→기존 dispatch→request_render→실제 어댑터/renderer→최종 조립까지 한 번 실행했다. 운영 HTTP/원격 executor를 실행했다는 증거는 아니다.

| 항목 | 실제 기록 |
|---|---|
| plan id / 승인 revision | `0972b033-84e0-4b98-9b82-9068526fc839` / `1` |
| scene / shot | `walk` / `walk-shot` |
| run_key | `0a7aecf64192716adacaad04d7e11b97417a5d868dd90e1d0660064c124f8926` |
| producer MP4 SHA | `574049b4c3372bbe3ca0cecac634e034795d1128d524a11fcfc7d6852551d9e7` |
| 최종 MP4 SHA | `e772b981b219d1c72dd9b39ddeb095eed2fb834aa3852fb5c455a0922f868414` |
| 선택 보존 | 승인 director.scene / plan.scene / input.json / animation_spec.json / render_manifest.json 모두 linear |
| 실제 decode | producer·최종 각각 FFmpeg framemd5 192프레임, exit 0 |
| 실제 영상 | 최종 8.02초, 1280×720, 24fps; source manifest 8초·제작12/재생24fps |
| QA | segment succeeded, final qa_failed; video/audio track·해상도·길이·not_black pass, audible fail |
| 의미/시청/청감 | semantic/viewing unknown; 전체 시청·청취 미실행 |

무음 마스터 선택은 이 실험 script의 문제다. 기계 QA가 이를 제대로 거부했으며 backend 실패나 보간 미전달로 보고하지 않는다. producer의 합성 soundtrack은 최종 무음 마스터와 다르다. 새 음악 제작·음성·청감 교정은 범위 밖이므로 고치지 않았다. 최종 영상은 QA 실패 검토용으로 공유한다.

## 5. 소스 지문

| 경로 | 착수 SHA-256 | 종료 SHA-256 |
|---|---|---|
| planner.py | `9a62c69698f7599e74ad4b2acaf01713e855c886ae4e7552b9000be6855e8262` | `8b9f89d0d1242837bfb128f09f0b0c6a68849a1dfb7a7cb0abd913f79109c5c3` |
| dispatch.py | `af471ee219b91f93406ceb9f6a399161d61e1678a8240182f67b3fcf223fb834` | `ac2b5872a299e43ae71e191cfc92c9cf4f4fc611c7155e6f2291ae4af581bd08` |
| frame_animation.py | `f2c1ee2216dde8c9be3e2e8e0a10484c56671e139b47cfff6e90f94508f46cb4` | `dca9dbb1ecdbfba45ac23b3580b2ae087c86ed492f7d2c7892910ecbe027d5a7` |

현재 Run outbox는 `/Volumes/Backup/ttakkari-data/outbox/452cc3c0-e5b0-4157-b5cf-1ddca82787d5/interpolation`이다. start/final-source.json, 3개 자체 diff patch, preservation.json, red/green/regression/core-regression.log, actual-dispatch.log, collect.log, actual 입력·승인계획·manifest·receipt·결과·summary, 디코드 framemd5를 보존한다. 저장소 증거 폴더에는 작은 JSON/로그/patch/script만 복사하며 binary·전체 작업 폴더는 추가하지 않는다.

## 6. 남은 경계

전체 ENG/B는 부분이다. 실제 모델 보간 선택 추론·독립 의미/시청 평가·청취·UI 여정·원격 executor 운영 소비·통합 배포는 이번 작업에서 하지 않았다. 기존 A browser IPC 및 C bpy 차단을 재시도하거나 다른 engine/settings로 우회하지 않았다. A/C 실제 렌더 미완 상태도 유지한다. 보간 선택 전달 성공은 해당 차단 해소 또는 전체 콘텐츠 완성 증거가 아니다.

## 7. 공유 산출물

| 산출물 | share_file ID |
|---|---|
| 최종 MP4 — 무음으로 QA 실패, 검토용 | `1d4df697-7ad5-4fb4-adb6-f8538ac7983b` |
| 실제 producer MP4 — linear | `6eb383eb-bb3a-4d45-a642-db214afe0120` |

보고서·증거 ZIP도 현재 Run outbox에서 share_file로 공유한다. 보관함 저장은 요청받지 않아 하지 않는다. 마지막 점검에서 기록한 소스 지문 35/35가 일치했고, 변경 코어 3경로 외 소스 변경 없음과 diff-check exit 0을 재확인했다.
