# Reverse platform contract v2

프론트와 비교 실행기가 공유하는 API·저장 JSON 계약이다. 기존 `POST /api/reverse` 및 `/api/agent/reverse/start`, `GET /api/reverse`, `GET /api/reverse/{id}`, retry/messages/delete 경로와 workspace/session 소유권 검사는 유지한다. 기존 요청의 `description`만 보내면 기본값 `target_type=auto`, `mode=auto`, `max_repair_attempts=0`으로 호환 처리한다.

## 시작 요청

```http
POST /api/reverse
Content-Type: application/json
```

```json
{
  "description": "CLI `slugify` 명령이 basename을 정규화하는 동작을 재현한다. 입력 파일명과 옵션별 출력을 비교한다.",
  "title": "slugify basename 호환성",
  "target_type": "cli",
  "mode": "analyze_reconstruct",
  "references": ["https://example.test/slugify", "fixtures/names.json"],
  "max_repair_attempts": 2,
  "workspace_id": "018f39b2-4b21-7d2b-98f0-69344241b88a"
}
```

`target_type`: `auto|cli|web|api|source`; `mode`: `auto|analyze|reconstruct|analyze_reconstruct`; `max_repair_attempts`: 정수 `0..3`. `title`은 선택값이다. `references`는 URL 또는 사용자가 설명에 제공한 로컬 참조 식별자이며 접근 불가한 참조는 제거하지 말고 사유와 함께 미확인으로 기록한다. 서버는 받은 type/mode와 추천값·추천 근거·실행 계획을 반환한다. 사용자가 type/mode/title을 바꿔 재요청할 수 있다. analyze 결과를 reconstruct로 전환할 때 기존 observation/evidence를 전달해 재사용한다.

## 비교 기록

```json
{
  "schema_version": 2,
  "job": {"title": "slugify basename 호환성", "target_type": "cli", "mode": "analyze_reconstruct"},
  "recommendation": {"target_type": "cli", "mode": "analyze_reconstruct", "reasons": ["실행 가능한 명령과 입출력 예시가 제공됨"]},
  "plan": ["원본 관찰", "구현", "직접 비교", "독립 검증"],
  "latest": {
    "source": {"status": "verified", "files": ["src/main.py"]},
    "execution": {"status": "verified", "checks": [{"command": "python -m pytest -q", "exit_code": 0, "log": ".reverse/runs/run-1/tests.log"}]},
    "direct_comparison": {"status": "mismatch", "count": 1, "matched": 0, "mismatched": 1, "unverified": 0},
    "independent_verification": {"status": "pending", "run_id": null, "verdict": null},
    "comparisons": [{
      "id": "basename-space", "behavior": "공백 포함 basename 정규화", "target_type": "cli",
      "input": {"argv": ["slugify", "two words.txt"]}, "pre_state": {"cwd": "/sandbox/case-1"},
      "original": {"available": true, "stdout": "two-words\n", "stderr": "", "exit_code": 0},
      "replica": {"stdout": "two_words\n", "stderr": "", "exit_code": 0},
      "environment": {"os": "linux", "runtime": "python 3.13"}, "evidence_level": "direct",
      "evidence": [{"kind": "execution", "path": ".reverse/comparisons/basename-space.json", "sha256": "<sha256>"}],
      "verified_at": "2026-11-07T00:00:00Z", "run_id": "<implementation-run-id>",
      "result": "mismatch", "difference": {"field": "stdout", "original": "two-words\n", "replica": "two_words\n"}
    }]
  },
  "history": []
}
```

각 비교는 정확히 `id, behavior, target_type, input, pre_state, original, replica, environment, evidence_level, evidence, verified_at, run_id, result, difference` 필드를 갖는다. 원본을 실행할 수 없으면 `original: null`, `result: "unverified"`, `difference.reason`과 `evidence`의 실패 관찰을 보존한다. 허용된 result는 `match|mismatch|unverified`; evidence level은 `direct|observed|inferred`다. `latest`는 최신 회차의 요약이며 새 구현/검증 회차는 이전 `latest` 전체를 `history[]`에 append한 뒤 교체한다.

원시 실행 증거 JSON은 `schema_version:2, target_type, input, pre_state, environment, original, replica, execution_log, verified_at, run_id, complete`를 담는다. CLI 실행은 `python -m ttakkari.reverse_compare --workspace <workspace> --spec <json>`로 캡처한다. API는 `run_api_comparison`으로 allowlist된 호스트만 호출하고 응답 본문·상태·상태 전후를 캡처한다. 웹 관찰은 별도 browser 도구의 원본·복제 관찰을 보존하되 실제 브라우저 실행 결과를 서버가 확인할 수 없으면 미검증이다. 명령 실행 timeout·원본 실행 불가 시 원본은 null, complete=false, execution_log.error에 이유를 둔다.

서버는 선언된 상대 경로의 정규화·심링크/탈출·민감 경로 차단, 일반 파일·크기 상한·SHA-256·JSON, `run_id`, `target_type`, 입력·사전 상태·환경·출력 필드의 상호 일치와 `complete`를 확인한다. CLI는 stdout/stderr/exit_code, API는 status/body/headers/state를 원시 캡처에서 재계산한다. 서버가 파일을 실제로 읽어 결과를 다시 만들어야 `direct`로 인정한다. 에이전트가 쓴 `result`, `passed`, 종료 문구는 판정 근거가 아니다. 비교가 없으면 직접 비교는 0건·unverified다. 독립 검증 상태는 별도 세션의 실재·검사 증거·verdict로만 갱신한다.

## 응답 예시

```json
{
  "id": "018f39b2-4b21-7d2b-98f0-69344241b88a", "kind": "reconstruction",
  "title": "slugify basename 호환성", "target_type": "cli", "mode": "analyze_reconstruct",
  "status": "queued", "stage": "request",
  "recommendation": {"target_type": "cli", "mode": "analyze_reconstruct", "reasons": ["실행 가능한 명령이 제공됨"]},
  "plan": ["원본 관찰", "구현", "직접 비교", "독립 검증"],
  "result": {"schema_version": 2, "latest": null, "history": []}
}
```

상태는 `source`, `execution`, `direct_comparison`, `independent_verification` 별도로 보인다. `done`은 필수 단계가 증거로 검증되고 독립 검증이 통과한 경우에만 허용한다. 분석 모드는 소스 없이 observation/hypothesis/unknowns로 완료 가능하며 재현 완료로 해석하지 않는다. 기존 report 조회는 유지하고 v2 비교가 없는 레거시 결과에는 `direct_comparison: {status:"unverified", count:0}`를 표시한다. 전체 정확도 점수는 제공하지 않는다.
