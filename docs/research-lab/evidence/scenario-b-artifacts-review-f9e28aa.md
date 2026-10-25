# Stage 2 Scenario B 및 artifact 경로 독립 재검증

- 검토일: 2026-10-25 (KST)
- 검토 대상 revision: `f9e28aa8cc2a6461f22d34f086be14c481992b98`
- 고정 방법: 원 checkout과 분리한 detached worktree `/private/tmp/rv2-review-f9e28aa`
- 검사자 실행 경로: Codex command sandbox, macOS, Python 3.14.7. 실행 모델의 세부 model ID는 런타임에서 확인할 수 없음.
- 코드 변경: 제품 코드 및 `main`/`back`/`front` 원본 수정 없음. 본 문서만 검토 evidence로 추가.

## 판정

| 항목 | 판정 | 근거/한계 |
|---|---|---|
| Baseline 대 Candidate 정규화 설정 효과 | **비교 PASS 불인정** | 두 입력에서 baseline 옵션 결과를 `chunk_text()`에 넣으면 candidate 옵션 청크와 같아지는 것을 직접 재현했다. 제공 JSON의 같은 chunk WAV SHA-256도 각각 동일하다. |
| `adapter_res.artifacts` → 등록 → `ResearchArtifact` 코드 연결 | **정적 경로 확인** | 지정 revision의 runner가 `result.artifacts`와 `raw_output.artifacts`를 합친 뒤 path를 등록하고 `ResearchArtifact`를 추가한다. DB 기반 회귀 테스트는 실행하지 못했다. |
| 누락 파일·중복 항목·legacy `artifact_id` | **코드 동작 확인, 런타임 미검증** | 누락 파일은 조용히 건너뛰며, 중복 제거는 dict 완전 동일성 기준이고, legacy ID는 UUID 연결만 한다. 격리 DB identity가 맞지 않아 런타임 재현은 하지 않았다. |
| 영구 파일 및 새 ASGI 요청의 metadata/content API | **런타임 미검증** | 코드와 좁은 테스트가 정상 경로를 구성하지만 테스트 DB를 사용할 수 없어 실제 DB·파일 저장·HTTP 응답은 새로 확인하지 못했다. |

## 검사 범위와 실행

대조한 자료는 `integration-stage2-b-supplement.md`, `stage2-b-supplement-evidence.json`, `run_scenario_b_execution.py`다. 대상 revision의 `runner.py`, `tts_synthesis.py`, `tts/normalize.py`, `tts/worker_supertonic.py`, artifact service/API 및 `tests/test_scenario_b_artifacts.py`도 읽었다. 다른 Run outbox의 파일은 열지 않았다.

### TTS 설정 반례 재현

실행 명령:

```sh
PYTHONPATH=src python3 - <<'PY'
from ttakkari.research.adapters.tts_synthesis import _normalize_with_options
from ttakkari.tts.normalize import chunk_text
texts = ["2026년 10월 25일 오후 2시 30분에 연구실 회의가 시작됩니다.",
         "HTTP 200 OK 응답과 함께 15.5MB의 JSON 데이터가 성공적으로 반환되었습니다."]
d = {"JSON": "제이슨", "HTTP": "에이치티티피"}
for t in texts:
    b = _normalize_with_options(t, use_convert_numbers=False, use_read_latin=False, custom_dict={})
    c = _normalize_with_options(t, use_convert_numbers=True, use_read_latin=True, custom_dict=d)
    print(b == c, chunk_text(b, 220) == chunk_text(c, 220))
PY
```

실제 출력:

```text
False True
False True
```

해석: adapter는 옵션 정규화 결과 `norm_s`를 `chunk_text(norm_s, ...)`에 넘긴다. `chunk_text()`는 내부에서 옵션과 무관하게 `normalize_text()`를 호출하고, 여기서 숫자 변환과 라틴 문자 읽기가 다시 적용된다. 지정 두 입력 모두 baseline의 원문 입력은 이 단계에서 candidate의 청크와 같아졌다. Supertonic worker는 받은 chunk 문자열을 그대로 `engine.synthesize(text, ...)`에 넘기므로, 이 실험에서 의도한 정규화 옵션의 음성 입력 차이는 성립하지 않는다.

제공 JSON을 직접 대조한 출력:

```text
chunk=0: sha_equal=True; size_equal=True
chunk=1: sha_equal=True; size_equal=True
```

따라서 WAV 동일성은 문서에서 가져온 기존 증거의 교차 일치이며 이번 검토의 새 합성 결과는 아니다. chunk 동일성·seed 산정 코드상 실제 deterministic 합성도 같은 결과일 것이라는 결론은 **코드 기반 추론**이다. Supertonic/Whisper를 이번 검토에서 실행하지 않았다.

### Artifact 저장 및 ASGI 경로

- `runner.execute_run_instance`는 `adapter_res.artifacts`를 먼저 모으고, list인 `raw_output.artifacts` 중 현재 목록에 완전히 같은 값이 없는 항목을 추가한다. 따라서 `raw_output`과 `artifacts` 사이 중복은 제거하지만, `adapter_res.artifacts` 내부 중복은 이 코드에서 제거하지 않는다.
- `artifact_id`가 없는 항목은 `path`가 실제 파일일 때만 `register_file(..., force_copy=True, run_id=None)`를 호출한다. service는 새 UUID 디렉터리로 파일을 복사해 `Artifact` 행을 추가하고, runner가 그 ID로 `ResearchArtifact`를 추가한다.
- path가 없거나 파일이 사라진 경우 등록 없이 다음 항목으로 진행한다. 별도 오류나 경고가 없어 adapter가 성공을 반환하면 Run 성공과 artifact 0건이 함께 가능하다.
- 같은 파일 경로라도 메타데이터가 달라 dict 비교에서 같지 않으면 각각 등록될 수 있다. 각 `register_file`은 새 UUID를 발급하므로 파일·`ResearchArtifact` 중복 생성이 가능하다.
- legacy non-empty `artifact_id`는 UUID 변환 후 `ResearchArtifact`에 바로 연결하며 저장 파일을 복사하거나 확인하지 않는다. 대상 Artifact 존재/외래키 동작과 콘텐츠 가용성은 DB 조건에 달려 있으며 이번에는 검증하지 않았다.
- metadata endpoint는 DB에서 Artifact를 다시 조회한다. content endpoint도 Artifact를 조회한 뒤 `content_path()`로 파일을 검증하고 `FileResponse`를 만든다. FastAPI의 `get_db()` dependency는 요청마다 `sessionmaker()()`를 여므로 metadata와 content의 별도 ASGI 요청은 새 SQLAlchemy session 경로다.
- `tests/test_scenario_b_artifacts.py`는 정상 path 한 개의 복사·연결·metadata/content API를 다루지만 missing path, 중복 항목, legacy ID 반례를 다루지 않는다.

### 제공 evidence의 교차 불일치

Markdown 표의 `ResearchArtifact ID`와 `Artifact ID`가 같은 문서의 sanitized JSON 값과 다르다. 예를 들어 baseline chunk 0의 Markdown은 `8b5ff3f8… / f2fc8c52…`, JSON은 `5274f149… / 7b566d18…`다. 네 WAV의 크기와 SHA-256은 chunk별 Markdown/JSON 간 일치한다. 그러므로 Markdown 표의 ID를 실행 식별자로 재사용할 수 없고, JSON도 이번에 DB 조회로 확인된 값은 아니다.

## 격리 조건, 환경, 미검증

- `TTAKKARI_TEST_DB`는 URL 문자열을 출력하지 않고 표준 주입값의 identity만 검사했다. database는 `ttakkari_test`였으나 user가 `ttk_test`가 아니어서 **MISMATCH**였다. 지시에 따라 해당 DB를 사용하지 않았다.
- 좁은 `tests/test_scenario_b_artifacts.py` 및 runner/adapter 테스트는 실행하지 않았다. 현재 표준 주입값 identity가 불일치하고, 기본 `python3`에는 SQLAlchemy가 없어 DB 기반 테스트 환경을 구성할 수 없다. broad test suite는 실행하지 않았다.
- 검토자 직접 실행은 `_normalize_with_options`/`chunk_text` 반례만 해당한다. script에 적힌 실제 Supertonic 3 ONNX CPU 합성과 Whisper local worker CPU proxy 호출은 원 evidence의 주장일 뿐, 이번 검토에서 새로 확인한 실행 사실이 아니다.
- 코드상 라우팅은 `TtsSynthesisAdapter` → `SupertonicProvider` → `ttakkari.tts.worker_supertonic`이며, STT 평가 경로는 `meetings.local_stt.runner.run_worker(..., production=True, language=ko)`다. 실행 당시 선택된 model revision, worker 프로세스, 장치와 실제 출력은 미검증이다.
- 작업 중 공유 checkout HEAD가 `f9e28aa…`에서 `8b9d622…`, 이어 `4049353…`으로 이동했다. 본 검토는 detached worktree에서 계속 `f9e28aa…`만 대상으로 했다. 이후 correction revision의 내용 및 영향은 검토 범위 밖이다.
- 환경 probe 명령은 Codex sandbox에서 macOS capability 결과를 출력했다. 결과 등록 도구는 첫 요청 형식 오류 뒤 `challenge is unknown`을 반환해 MCP 등록은 완료되지 않았다. 따라서 probe 결과는 로컬 실행 evidence로만 기록한다.

검토에서 실행한 독립 TTS 재현의 핵심 출력은 위 두 줄이며, DB/API 및 모델 실행은 **미검증**이다. 이 보고서는 전체 통합 완료나 배포를 주장하지 않는다.
