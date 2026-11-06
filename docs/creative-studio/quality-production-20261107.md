# 의미·시청 QA와 부분 수정 — 실행·감사 증거

상태: 부분. 품질 계약·로컬 미디어 전달 모듈·반례 테스트를 구현했다. core가 추가한 dispatch 연결을 실제 합성 미디어 렌더 테스트로 확인했다. 독립 모델의 전체 영상 평가, 청취, 실제 콘텐츠의 부분 재제작은 실행하지 않았다. 기계 QA 통과를 의미·시청 품질 통과로 올리지 않는다.

- 작업 트리: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`
- 착수 기준 전체 SHA: `5f5cbd997addcb4ea084ab908790f8f6eec54122`. 감사 증거 갱신 시 HEAD: `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`. 담당 4파일은 여전히 untracked이며 이 세션은 지시대로 commit/push하지 않았다. HEAD만으로 미커밋 품질 모듈의 버전을 고정하지 않으므로 공유 묶음에 파일별 SHA256도 남긴다.
- 소유 파일: `src/ttakkari/creative/v2/quality.py`, `quality_worker.py`, `unit_tests/test_creative_v2_quality.py`, 이 문서.
- 현재 Run 산출물: `/Volumes/Backup/ttakkari-data/outbox/75e8be20-5207-4823-bab2-7e25b2ee470b/quality-audit/`. 이전 Run outbox를 읽거나 쓰지 않고 프로젝트 원본에서 다시 준비했다. 초기 Run 실행 기록은 아래 4·5절에 구분해 보존한다. 최신 증거 색인은 6절이다.
- 작업 카드의 기존 진행은 읽기 조사까지였다. 담당 품질 파일이 없음을 `git status --short`와 파일 목록으로 확인한 뒤 구현했다. 타 담당자 변경은 유지했다.

## 1. 완료 기준 대조

| 기준 | 구현·관측 | 남은 작업 |
|---|---|---|
| 1. 장르별 사전 기준·결함 근거 | `build_rubric(plan)`은 C01/C12 선택과 계획 내용 해시를 고정한다. scene/shot 및 프레임·시각을 평가·수정 기록에 보존한다. core의 `_new_record`가 렌더 전 rubric을 기록함을 통합 테스트로 확인했다. | 이미 제작된 Benchmark B의 평가 범위는 사후에 구성했으므로 그 영상의 사전 기준 적용 증거가 아니다. |
| 2. 3단계·불확실성·평가 출처 | `machine_qa`, `semantic_qa`, `viewing_qa`를 분리한다. 근거 없음은 unknown/uncertainty=1. 평가자 id/version/source/independent와 관측 근거를 보존한다. | 독립 evaluator 실실행·확률 보정은 미실행. |
| 3. 부분 수정·반복 실패·캐시 | 결함 코드별 layer/전략, 2회 실패 후 대안 전략, 대안도 2회 실패하면 자동 반복 중단 계약. 좋은 scene/shot/asset/성공 segment run_key 보존 계약. | 실제 자산 재생성과 구간 조립 실행은 core 소유다. |
| 4. 호출 계약 전달 | 총괄 세션에 `ttk post /api/sessions/<총괄>/runs`로 함수 시그니처와 입력 계약을 전달했다. 첫 접수 ID `79c3bdf3-33e5-47d0-8c33-cf5dce93612a`, 후속 `1ff5d934-09da-4755-a0c2-68416cf35f90`. 이후 core의 실제 dispatch 호출·영속화를 별도 테스트로 확인했다. | 원 작업 시작부터 15분 이내였는지는 인계에 원착수 시각이 없어 미확인이다. |
| 5. 실제 미디어 입력 전달 | 실제 기존 10초 영상에서 PNG 15장과 WAV를 추출했다. 바이트 전달 테스트는 실제 MP4/PNG/WAV가 evaluator callable 입력에 도달함을 확인한다. 직접 표본 3장도 열었다. | 실제 독립 평가 하네스 호출과 전체 재생·청취는 미실행. 테스트 callable을 모델 실행으로 주장하지 않는다. |
| 6. 5종 결함·재평가 반례 | 가짜 문자·캐릭터·모션·음성·전환별 실패→부분 수정 계획→출력 해시 변경→구 검수 무효→새 검수 반영 테스트. | 합성 receipt 계약 테스트이며 실제 영상 개선 제작 증거가 아니다. |

## 2. core 연결 계약

```python
from ttakkari.creative.v2.quality import build_rubric, evaluate_production, plan_partial_repair
from ttakkari.creative.v2.quality_worker import prepare_review, deliver_review

# 승인된 계획을 렌더하기 전에 core의 제작 기록 생성 단계에서:
record["rubric"] = build_rubric(plan)

# 최종 미디어 경로는 PlanStore 기준으로 해석해서 전달한다.
# 현재 dispatch.final.file은 계획 폴더 상대경로다.
report = evaluate_production(plan, {**record, "assets": production_data["assets"]},
                             media_path=absolute_final_path)

# 독립 하네스에 넘길 실제 파일·요청 준비. 준비만으로 평가가 실행되지는 않는다.
package = prepare_review(plan, absolute_final_path, current_run_review_dir)
# 기존 평가기 구현이 있을 때만 명시적으로 연결한다. 기본·가짜 평가기는 없다.
receipt = deliver_review(package, existing_independent_evaluator)
record.setdefault("quality_evidence", []).append(receipt)
```

`evaluate_production(plan, record, *, media_path=None)`은 저장·dispatch·결제·사용자 승인을 수행하지 않는다. 반환 필드는 `rubric`, `output_sha256`, `rubric_matches_record`, `rubric_timing`, `machine_qa`, `semantic_qa`, `viewing_qa`, `defects`, `repair_plan`, `overall_verdict`다. `rubric_matches_record`는 스냅샷 일치만 나타낸다. evaluator는 생성 시각을 입증하지 않으므로 `rubric_timing=not_attested_by_evaluator`를 유지한다. 렌더 전 생성 이력은 core의 실제 이벤트로 기록해야 한다.

`record.quality_evidence`는 신뢰된 평가 실행 경로가 기록하는 배열이다. 클라이언트가 보내는 임의의 `independent:true`를 인증된 평가 출처로 승격하면 안 된다. 이 모듈은 평가자 인증 서버가 아니며, Director의 기존 `cpu-check` ReviewReceipt나 단순 registry 등록을 독립 관측으로 변환하지 않는다.

```json
{
  "stage": "semantic_qa",
  "output_sha256": "실제 영상 SHA256",
  "rubric_sha256": "build_rubric 결과 SHA256",
  "evaluator": {"id": "실제 평가자", "version": "실제 버전", "source": "실행 출처", "independent": true},
  "criteria": [{
    "criterion_id": "text_legibility", "scene_ids": ["s1"],
    "verdict": "pass", "uncertainty": 0.1,
    "evidence": [{"path": "/실제/표본.png", "sha256": "프레임 SHA256",
      "source_sha256": "원본 영상 SHA256", "kind": "frame", "start_time": 1.0, "end_time": 1.0}]
  }],
  "defects": []
}
```

`stage`는 `semantic_qa` 또는 `viewing_qa`, `verdict`는 pass/fail/unknown이다. 그림은 frame/video, 시간에 따른 동작은 video, 청취는 audio 근거가 필요하다. 사실·시대·원본 충실도는 reference 파일도 필요하다. 관련 장면의 시간 범위를 덮지 않는 근거, 누락 기준/장면, 오래된 계획·영상 해시, 불명확한 evaluator, 잘못된 구조·높은 불확실성은 pass를 만들지 않는다. `max_pass_uncertainty=0.25`는 명시적 초기 수락 정책이며 경험적으로 보정한 품질 점수가 아니다.

`prepare_review`는 장면 사분위·시작/마지막 프레임·샷 중간을 추출하고 전체 영상을 보존한다. 프레임 상한을 넘으면 장면을 몰래 누락하지 않고 분할을 요구한다. 동일 출력/기준으로 재호출하면 해시 검증 후 기존 패키지를 돌려주며 다른 출력으로 덮어쓰지 않는다. `deliver_review`는 원본·추출 파일 해시를 검증하고 MP4/PNG/WAV 바이트를 callable에 전달한다. transport receipt는 callable에 바이트를 전달했다는 사실만 입증한다. 모델이 영상·소리를 실제로 해석했는지는 해당 하네스의 실행 관측으로 별도 기록해야 한다.

`plan_partial_repair(plan, record, defects)`는 입력을 바꾸지 않는다. `actions`에 결함·scene/shot/layer·전략·근거·재평가 3단계를 반환한다. `invalidate`는 재생성 레이어/자산·조립할 구간·최종 결과·구 검수 무효화, `preserve`는 다른 자산·좋은 샷·성공 구간 run_key다. 구간 조립은 좋은 샷을 포함할 수 있지만 자산 생성은 보존 목록을 재사용해야 한다. 알 수 없는 대상·layer 충돌·지원하지 않는 결함은 blocked로 남기며 전체 무효화를 대신하지 않는다. 전환이 이웃 장면까지 영향을 주거나 타임라인 길이를 바꾸면 core가 기존 run-key/시각 의존성을 함께 재계산해야 한다.

## 3. 기존 경로 조사

- 작업 트리 `registry_data.py`의 `model.multimodal_judge`: known_missing=True, integration=none. 이는 이 소스 트리의 상태이며 운영 배포의 현재 능력을 검사한 결과는 아니다.
- `engines.qa_media`: 실제 FFmpeg 트랙·해상도·길이·검은 화면·음량 검사를 재사용했다.
- `director.py`의 `ReviewReceipt`와 `create_review`: 출력 해시와 pass/fail/unknown 기준은 있지만, cpu-check/independent=False 및 빈 evidence_uris다. 이 기록을 시청 품질 pass로 재사용하지 않는다.
- recall에서 2026-10-21 AI-native 스튜디오 결정과 2026-10-20 추가 유료 과금 금지를 확인했다. indexed work-history 검색도 실행했다.
- 지식 도서관 `kb_search("creative multimodal quality judge")`는 이 연결에 쓸 직접 근거를 반환하지 않았다. 새 서버를 만들지 않고 실제 파일을 기존 독립 GPT/Claude/사람 검수 경로에 전달하는 최소 계약을 제공했다.

## 4. 초기 Run 실제 산출물과 실행 증거 (4d1a3f58)

프로젝트의 접근 가능한 정본을 사용했다. 소스 스토리보드: `templates/frame_animation/benchmark_b_storyboard.json`; 실제 영상: `outbox/benchmark_b_work/run_02_remediated/ttakkari_2d_animation_final.mp4`. 기존 제작물을 복사해 평가 입력을 준비했으며 새 애니메이션을 제작했다고 주장하지 않는다.

- 영상 SHA256: `30213960c0c8e71d5bff3bf0649d4a3383c4ccfacde73a9c5e073080316aa18e`.
- 초기 Run `4d1a3f58-d713-4373-ba6b-efa96fc65ea6`의 `quality/benchmark-b/`: `review-plan.json`, `benchmark-b-source.mp4`, `inputs/review-request.json`, 프레임 15 PNG, `inputs/listening.wav`, `unreviewed-quality-report.json`, `sampled-visual-observation.json`. 현재 세션에서 그 outbox 원본을 재열람한 것은 아니다.
- actual report: machine=pass, semantic=unknown, viewing=unknown, overall=unknown.
- `view_image`로 frame-0002/0007/0012(2.0/5.5/8.5초)를 열었다. 표본에서 한국어 자막과 캐릭터의 표정·자세 변화를 관측했다. 구현자가 본 세 장의 정지 화면이므로 independent=False이며 전체 재생·청취 증거가 아니다.

재현 준비 명령(이미 준비된 같은 입력은 재사용):

```sh
.venv/bin/python -m ttakkari.creative.v2.quality_worker \
  <현재-Run>/quality/benchmark-b/review-plan.json \
  <현재-Run>/quality/benchmark-b/benchmark-b-source.mp4 \
  <현재-Run>/quality/benchmark-b/inputs
```

관측: `status=prepared_not_evaluated`, frames=15, evaluator_state=not_executed. 준비 함수·실제 FFmpeg 검사도 Python에서 실행해 위 SHA와 unknown 유지 결과를 확인했다.

검증 명령:

```sh
.venv/bin/python -m pytest unit_tests/test_creative_v2_quality.py -q
.venv/bin/python -m pytest unit_tests/test_creative_v2_quality.py unit_tests/test_creative_director.py unit_tests/test_creative_v2_dispatch.py -q
.venv/bin/python -m ruff check src/ttakkari/creative/v2/quality.py src/ttakkari/creative/v2/quality_worker.py unit_tests/test_creative_v2_quality.py
.venv/bin/python -m ruff format --check src/ttakkari/creative/v2/quality.py src/ttakkari/creative/v2/quality_worker.py unit_tests/test_creative_v2_quality.py
```

초기 Run의 전용 테스트는 36개 계약 검사 후 core 통합 검사 1개를 추가했고, 그 Run의 마지막 전체 전용 검사 출력은 `37 passed in 6.73s`였다(`quality/pytest-quality.txt`). RED에서 모듈 부재, 장면 시간 커버리지 결함, 샷 자산 보존 결함, malformed receipt, 패키지 변경 반례가 각각 실패하는 것을 본 뒤 구현했다. 당시 관련 회귀 출력: `77 passed in 94.87s (0:01:34)`이며 초기 Run `quality/pytest-regression.txt`에 원문을 보존했다. 이 회귀 뒤 추가한 `-k core_dispatch` 검사는 `1 passed, 36 deselected in 4.26s`였다. 당시 ruff: `All checks passed!`, `3 files already formatted`. dev-python pitfall_scan: `total: 0 finding(s)`. `compileall -q`는 exit 0이었다. 이 문단은 과거 실행 기록이며 최신 재검증은 6절이다.

추가 통합 검사는 승인 계획→실제 자산 업로드→dispatch→FFmpeg 렌더→조립된 MP4→quality.evaluate_production→production.json 저장→view 노출을 실행했다. 평가 호출에 실제 파일이 존재하고 출력 SHA가 일치하며, 렌더 전에 저장한 rubric과 평가 시 rubric이 일치하고, 기계 pass여도 최종 품질은 unknown임을 확인했다. 합성 이미지·사인파로 기존 렌더러를 시험한 것이며 콘텐츠 제작 품질의 증거는 아니다.

미실행: mypy는 이 작업 트리 `.venv/bin/python -m mypy --version`에서 `No module named mypy`여서 실행하지 않았다. 새 의존성은 설치하지 않았다.

## 5. 범위와 남은 연결

이 모듈을 작성한 사람이 전체 품질을 독립 통과시킨 것으로 기록하지 않는다. 실제 독립 평가기·청취 경로 연결, HTTP API 인증 경로 확인, 부분 수정 실행과 실제 개선 전후 영상 비교, 배포·커밋·푸시는 미실행이다. dispatch/API는 core 담당 소유라 여기서 편집하지 않았다. dispatch 영속화 연결은 테스트로 확인했지만 공유 작업 트리의 전체 통합 완료를 판정하지 않는다. 6절 갱신 시 core의 `evaluate_production` 호출에 제작 데이터 assets를 포함한 것을 확인했다. 총괄은 이 문서와 현재 Run 공유 파일을 감사자에게 인계할 수 있다.

초기 공유 영상 사본: artifact `d5555554-0b3e-4b96-8a14-ea78a809ee19` (위 SHA, 405,017 bytes). 초기 ZIP artifact `9763b5e4-a296-452e-a018-2db868731d2f`에 구현·테스트 스냅샷, 입력 계획·영상·프레임·WAV·평가 요청·unknown 보고·테스트 로그를 보존했다. 해당 공유 색인은 artifact `48c5b159-b343-4e90-b472-d0171df96eeb`다. 최신 묶음은 아래 6절을 우선한다.

## 6. 감사 인계 추가 조건에 따른 최신 증거 색인 (75e8be20)

이 절의 확인 대상은 구현자 자체 검증이다. 독립 감사 세션 `4b2802ba-c13f-4068-a2bf-18faacfbc229`의 역할을 대신하지 않는다. 총괄이 통합·배포·자체 검증을 확인한 뒤 그 세션에 인계한다. 이 담당자는 배포하거나 감사 완료를 선언하지 않았다.

증거 수집 시 전체 HEAD는 `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`다. 담당 파일 상태는 모두 `??`였다. `quality-audit/source-provenance.json`은 구현·테스트·입력·엔진 스냅샷별 원본 저장소 경로, 패키지 경로, SHA256과 바이트 수를 기록한다. `quality-audit-index.json`은 묶음 전체 파일별 SHA256을 기록한다.

현재 Run 루트: `/Volumes/Backup/ttakkari-data/outbox/75e8be20-5207-4823-bab2-7e25b2ee470b`.

| 대상 | 현재 Run / 프로젝트 경로 | 공유 접근 |
|---|---|---|
| 실제 기존 영상 사본 | `quality-audit/video/benchmark-b-source.mp4` | artifact `82cf56eb-2b9d-4b3b-9a20-232c0054bdf4`, SHA `30213960c0c8e71d5bff3bf0649d4a3383c4ccfacde73a9c5e073080316aa18e` |
| 증거 전체 묶음 | `quality-audit-evidence.zip`, 45개 항목·2,772,599 bytes | artifact `168d578e-cf06-4c18-a973-a2a203acb77a`, SHA `aec8ec2fa741d43d84d10d3dda83d47a7d27c6a7e532cd5edf0e9003490718a0` |
| 품질 소스·전용 테스트 | 프로젝트 `src/ttakkari/creative/v2/quality.py`, `quality_worker.py`, `unit_tests/test_creative_v2_quality.py`; 묶음 `quality-audit/source/` | 위 ZIP에 스냅샷과 파일별 SHA256 포함 |
| 스토리보드·원 엔진 입력·manifest·9개 레이어 | `quality-audit/inputs/` | 위 ZIP에 실제 JSON·PNG 포함. 원본은 프로젝트 `templates/frame_animation/` 및 `outbox/benchmark_b_work/` |
| 엔진 제작 이력 | `quality-audit/engine-history.txt`, `source/current-frame-animation-engine.py`, `source/current-engine-benchmark-script.py` | 위 ZIP에 현재 엔진·제작 스크립트·git 이력 보존. 현재 소스로 원 영상을 재제작했다는 증거가 아님 |
| 실제 평가 입력 | `quality-audit/review-inputs/review-request.json`, `frame-0000.png`~`frame-0014.png`, `listening.wav` | 위 ZIP에 실제 파일·장면 시각·프레임·원본 해시 포함 |
| 오디오 검수 기록 | `quality-audit/audio-review-record.json` | 추출·기계 QA 실행, 청취 미실행·발음/음악 적합성/AV sync unknown을 명시 |
| 실제 품질 보고 | `quality-audit/quality-report.json` | machine=pass, semantic/viewing/overall=unknown |
| 재현 명령·실행 출력 | `prepare-audit-evidence.py`, `quality-prepare-output.txt`, `quality-pytest.txt` | 위 ZIP에 실행한 스크립트와 실제 원문 출력 포함 |

이번 Run에서 실행한 명령과 관측:

```sh
git rev-parse HEAD
# 439472b3bc4665edc04ad1d7fa8ad1a9684a47b4
.venv/bin/python -m pytest unit_tests/test_creative_v2_quality.py -q
# 37 passed in 5.04s
.venv/bin/python /Volumes/Backup/ttakkari-data/outbox/75e8be20-5207-4823-bab2-7e25b2ee470b/prepare-audit-evidence.py
# frames=15 audio_extracted=True
# machine=pass semantic=unknown viewing=unknown overall=unknown
.venv/bin/python -m ruff check src/ttakkari/creative/v2/quality.py src/ttakkari/creative/v2/quality_worker.py unit_tests/test_creative_v2_quality.py
# All checks passed!
```

원 영상은 재렌더하지 않고 권한 있는 프로젝트 원본에서 복사·해시 검증했다. 원 영상 제작 당시 코드 버전 전체를 이 실행이 입증하는 것은 아니므로 현재 엔진 스냅샷과 원 영상의 생성 이력을 구분한다. 이번에는 추출한 전체 15장을 새로 시각 검토하거나 WAV를 청취하지 않았다. 초기 Run의 표본 3장 관측은 4절의 과거 기록으로만 남긴다.

미완료: 실제 독립 의미·시청 평가 및 청취, 실제 콘텐츠 결함의 부분 재제작·개선 전후 비교, HTTP 인증 경로·운영 배포 검증, 총괄의 감사 인계. 이들은 테스트 통과나 공유 파일 등록으로 완료 처리하지 않았다.

알려진 한계: evaluator의 독립성 인증은 신뢰된 호출 경로가 담당하며 이 모듈은 인증 서버가 아니다. uncertainty 수락 정책 0.25는 미보정 초기 정책이다. 지원하지 않는 결함 전략은 blocked로 남긴다. 이번 전용 검사에서 실패는 0건이었으나 전체 결함 부재를 판정한 것은 아니다.
