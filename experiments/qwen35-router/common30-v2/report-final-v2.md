# Qwen 라우터 공통 30문항 재비교 — 최종 보고

- 실행: 2026-10-16 KST, 소유 서버 2개 run 각 90요청(30문항 × 3회), CPU-only.
- 결과 해석: **9B를 대체할 근거 부족; 운영 교체는 보류.** 0.8B는 접수 모델 후보로도 현재 v2 기준을 통과하지 못했다. 30건 합성 평가만으로 최종 모델을 확정하지 않는다.
- 사용자 피드백: 실사용 확인 1건, 수정정책 정답분모 0. 합성 30문항을 실로그 분모에 합치지 않았다.

## 문항과 고정 조건

| 구분 | 분모/구성 | 고정 기준 |
|---|---:|---|
| 복원한 과거 평가 | 30개, CHAT 6 / STATUS 7 / LOOKUP 5 / WORK 12 | 원문 라벨과 스크립트를 그대로 보존 |
| 현재 접수 v2 | 30개, answer 6 / delegate 17 / abstain 6 / hold 1 | 세 행동 정책으로 출력 전 독립 고정 |

`common30-07`은 정책 근거가 충분히 고정되지 않아 사전 보류했고 `{'abstain', 'answer'}` 중 하나를 허용했다. 하드 exact 분모 87(29 × 3)에서는 제외했지만, 90회 전체의 allowed-set/coverage와 원자료에는 남겼다. CHAT/STATUS/LOOKUP/WORK를 세 행동으로 치환하지 않았고, 따라서 과거의 Luna·Haiku 수치와 현재 v2 수치는 별도 표다. 과거에는 작업판·최근대화 컨텍스트 없이 사용자 원문만 썼으므로 새로운 v2 기준선으로 간주할 수 없다.

- 과거 스크립트: `/private/tmp/claude-501/-Users-winterholic-development-projects/900a2c72-a605-4640-83d0-186262ead460/scratchpad/route_eval.py`; 파일시각 `2026-10-13T21:02:29.218526+09:00`, 2,945 bytes, git 추적/commit 없음, SHA256 `0805ed7f06962da0bf7c6509be614b056e313125aeb67bd51f2a006cc7ac6199`. 복사본 `legacy-route_eval-v1.py`.
- 과거 결과 기록(재실측 아님): Luna 30/30, p50 4.80s; Haiku 29/30, E2E median 7.36s; Kanana 1.5-8B Q4 23/30 두 번; Kanana 2-3B Q4 3/12, Q8 7/12(별도 12문항이라 30개와 합산 금지).
- 고정 데이터 SHA256 `37b607c9679bba193f18578256a33f8477a3f36cf30e05283ae3ce32ac95b2b0`; 시스템 프롬프트 SHA256 `850eb694411d25fe0d51c3960fe778a63503a74c022becd8ef14701816b5c1bd`; policy `qwen35-shadow-v2-manual-2026-10-15`. 요청 원문/후보 빈 목록/스키마/API용 동일 입력은 `hosted-comparison-inputs-v1.jsonl`에 동결했다.

## 새 실측

| 모델 | 문법/스키마 | exact 일치 | 허용행동 일치 | 비-abstain coverage | 반복 안정성 | 지연 |
|---|---:|---:|---:|---:|---:|---|
| Qwen3.5 0.8B Q4_K_M | 75/90 valid, 15 invalid (12 JSON 잘림/파싱, 3 검증 실패) | 15/87 = 17.2% | 18/90 = 20.0% | 0/90 valid | 행동 30/30 사례에서 abstain; 동일 5개 사례가 3회 모두 invalid | 요청 p50 976.54ms / p95 5897.94ms; TTFT p50 112.8ms / p95 124.0ms; cold-start 1.33s |
| Qwen3.5 9B Q4_K_M control | 90/90 valid, 0 invalid; 후보 membership 위반 0 | 63/87 = 72.4% | 66/90 = 73.3% | 90/90 | 30/30 사례에서 행동 안정 | 요청 p50 3107.88ms / p95 4174.37ms; TTFT p50 870.49ms / p95 953.02ms; cold-start 5.24s |

0.8B는 valid 출력 75건 전부 abstain했고 작업 접수 coverage가 0이었다. invalid 15건 원문과 개별 지연을 run2에 보존했다. 9B도 29개 확정 사례 중 21개만 exact였고 6/51 delegate 출력이 delegate 정답을 놓쳤다(3개 반복에서 총 6오류). 전 문항 행동은 반복 간 안정적이었다.

**의미·심각도 검토(9B):** 고정된 빈 작업판/이력에 없는 백엔드 재시작 승인 완료, 작업자 상태, 배포 상태, 세션 상태, 금요일 일정 등을 사실처럼 답한 S1 미근거 주장 5개 사례 × 3반복(15개 출력). 도구·후보 없이 실행 후속을 약속한 출력은 별도 위험 표식 6사례 × 3(18출력)이며 실제 실행은 없었다. MoE 질문 1개 사례는 답변으로 처리해 delegate 정책에서 벗어났고(반복 3회, 내용 자체는 대체로 맞음). shadow 출력이며 사용자에게 노출·실행되지 않았다. 0.8B의 잦은 abstain에는 권한·도구 부재를 지어내며 거절한 문구도 있어, 단순히 안전한 동작으로 보지 않았다.

## 자원·지연 한계

두 모델 모두 llama.cpp 동일 바이너리·서버 템플릿·context(4096)·CPU 6 thread·parallel 1·JSON constrained output·입력·decoding 조건으로 비교했다. 모델 quantization은 둘 다 Q4_K_M이며 파일/revision/hash는 manifest에 기록했다. Apple M5 Max/macOS 27.0, 64 GiB. GPU/Metal은 측정하지 않았다.

샘플링된 최소 available memory는 0.8B 40.28GB, 9B 35.94GB; 각 샘플의 pressure normal, swap 0. 서버 RSS 최대 관측 표본은 0.8B 1.97GB, 9B 약 10.46GB(샘플 기반이며 OS 전체 peak가 아님). 실험 중 9B 측정 일부에서 비소유 Python/Chrome 작업이 관측되어 지연 p50/p95를 공정한 우열 근거로 판정하지 않는다. 0.8B run2의 다른 프로세스는 표본상 낮은 CPU였으나 모델별 지연은 타부하 편차와 짧은 run 수를 고려해 참고만 한다. 완료 후 resource snapshot은 available 41.31GB, pressure normal, swap 0이었다.

## Luna / Haiku와 과거 기준

- 과거 Luna 30/30·Haiku 29/30은 조건이 다른 역사 기록이어서 현재 v2 결과에 넣지 않았다.
- Luna 공식 지원 경로를 통한 준비된 30회 Codex CLI 시도는 모델 응답 이전에 app-server 초기화가 `Operation not permitted (os error 1)`로 막혀 0개 응답을 받았다. 권한 확장 재시도는 하지 않았다. 따라서 Luna 현조건 점수는 **미측정**이다.
- Claude cap은 사용자 제공 snapshot상 직전 99%, reset 02:30 KST로 stale/가용 불명이라 Haiku를 호출하지 않았다(신규 API 과금 없음). 정확한 동일 system/user/schema/candidates/temperature/seed/token budget 입력과 Haiku CLI의 단일 호출 템플릿을 패키지에 넣었다. Claude Code CLI는 `--json-schema`와 `--system-prompt-file`, tools off 옵션을 지원한다([공식 CLI 문서](https://code.claude.com/docs/en/cli-usage)). Haiku는 **미측정**.

## 명령·안전·운영 상태

```sh
.venv/bin/python experiments/qwen35-router/common30-v2/supervise_server.py \
  --model /private/tmp/qwen35-router/model/Qwen3.5-0.8B-Q4_K_M.gguf \
  --model-label 0.8b-run2 --port 18787 \
  --output experiments/qwen35-router/common30-v2/results-qwen35-0.8b-cpu-run2.json \
  --owner-out experiments/qwen35-router/common30-v2/owner-qwen35-0.8b-run2.json
```

각 서버는 소유 PID로만 시작·종료되었고 child exit code 0, 공용 atomic lock은 소유자 검증 뒤 해제되어 현재 부재한다. 타 프로세스 종료, sudo/권한 우회, 외부서비스·설정 변경은 하지 않았다. 모델 추론은 CPU-only, 후보 ID는 전부 빈 배열이며 모델이 실제 모델/도구/권한을 만들 수 없다. observer 소스 default는 disabled 및 sampling 0.0; runtime env는 읽지 않았다. observer 준비 작업은 보존했다.

## 다음 단계

동일 조건/정답 검토/부하 격리로 표본을 늘리고 실사용 오류의 실제 severity를 관찰할 때까지 대체 판정은 보류한다. 0.8B는 현재 v2에서 비응답률과 invalid가 높아 접수 라우터로 채택하지 않는다. 성능 수치는 오염 가능 표시로 남기며 동등 조건에서 다시 계측하기 전 모델 지연 우열을 주장하지 않는다.

### 주요 출처

- [공식 Qwen3.5 0.8B model card](https://huggingface.co/Qwen/Qwen3.5-0.8B) (Apache-2.0)
- [Qwen3.5 0.8B GGUF 파일·revision](https://huggingface.co/lmstudio-community/Qwen3.5-0.8B-GGUF)
- [Qwen3.5 9B GGUF 파일·revision](https://huggingface.co/lmstudio-community/Qwen3.5-9B-GGUF)
- [llama.cpp 공식 프로젝트](https://github.com/ggml-org/llama.cpp)
- [GPT-6 Luna 공식 모델 페이지](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Claude Code 공식 CLI reference](https://code.claude.com/docs/en/cli-usage)

WBS: PMS task `1f5f8c49-15b8-4b08-9188-49410f7f5ea5` (`2.1.5.9`, `[QWEN07] 공통 30문항 경량 Qwen·9B 재비교`).
