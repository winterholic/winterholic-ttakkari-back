# 실제 CPU 모델의 소유자 종료 인수

2026-10-26 리드 Codex 직접. 실행 기준 d5bf5df7f366927d86f27f7cb96433ed4b444583.
원목표와 R4/7/12~16, PMS 6/7/8은 미완료다. 새 작업자를 만들지 않았다.

## 관측

| 실제 경로 | 종료 전 증거 | 시험 부모 SIGKILL 이후 |
|---|---|---|
| Supertonic CPU / Python3.11.16 / ORT1.31.0 | 실제 음성 173356 samples 반환, 로드한 모델로 추가 추론 반복 | 자식 부재 0.209초, failed/owner_lost, 정리 verified |
| Whisper large-v3 CPU int8 / Python3.13.15 / CTranslate2 4.8.3 | 기존 정상 WAV 재사용, 실제 전사 1 segment 반환, 추가 추론 반복 | 자식 부재 0.721초, cancelled/사용자 사유 보존, 정리 verified |

기존 공통 감독기·worker.main·실제 모델 라이브러리를 썼다. 시험 executable은 첫 실제
추론 결과 뒤 같은 실제 호출을 최대 30초 반복하여, 모델이 로드된 채 작업하는 동안
시험 부모를 종료할 수 있게 했다. 연구 어댑터는 이 감독기를 부르는 시험 seam이다.
전체 TtsSynthesisAdapter 여정이나 운영 DB 실험은 아니다. 종료 시점에 정확히 어느
native 호출/GIL 상태였는지 측정하지 않았으므로 native GIL fallback 발동으로 단정하지 않는다.
선행 모델 없는 PyDLL 반례/116검사는 소스8hash가 전부 일치하여 반복하지 않았다.

Run PostgreSQL은 conftest의 격리 DB다. 실제 DB identity를 seed_runs로 확인하고,
자식 입력 전 소유 UUID/PID/부모 checkpoint를 기존 runner가 기록한다. 첫 실추론 후
fresh 인증 ASGI GET으로 같은 신원을 읽고 살아 있는 소유자 복구 0을 검사했다.
부모 사망 후 별도 DB session과 fresh GET의 raw/env 일치, 원본 raw 유지, 사용자
취소 사유 보존, resume의 recovery event 1건, 복구 재호출 0을 검사했다.
DB 복구는 signals_sent=false의 관측만 수행하며 자식은 자체 부모 감시로 종료했다.
시험이 생성한 부모 handle만 kill했고 저장 PID에 신호를 보내지 않았다.
정리 결과가 기존 running checkpoint를 가짜 exit 영수증으로 바꾸지 않는 것도 확인했다.

## 실행 증거

```text
$ TTAKKARI_RESEARCH_LIVE_OWNER=1 RESEARCH_OWNER_LIVE_REPORT=<이번 Run outbox>/model-owner-live.json PYTHONPATH=src:tests .venv/bin/python -m pytest tests/research_smoke/model_owner_live_acceptance.py -q -s --timeout=240 --tb=short
actual ML owner crash: ttakkari.tts.worker_supertonic, owner_lost, exit=0.209s, cleanup=verified
actual ML owner crash: ttakkari.meetings.local_stt.worker, cancelled, exit=0.721s, cleanup=verified
2 passed in 15.37s
$ .venv/bin/ruff check tests/research_smoke/model_owner_live_acceptance.py
All checks passed!
```

opt-in smoke 파일은 기본 CI 수집 대상이 아니며 실제 모델 다운로드·설치가 없다.
이번에는 TTS owner_lost/STT 사용자취소 2건만 추가했다. 각 모듈의 양쪽 취소 행렬은
선행 모델 없는 native PG 4건에서 재사용하며 실제 모델 4건으로 숫자를 늘리지 않는다.
첫 실행의 dev/CORS 경고는 격리 시험 설정이며 운영 설정을 읽은 증거가 아니다.
원시 before/after/resume·runtime·request는 model-owner-live-d5bf5df7.json,
실행 전 자원·운영 readiness·배포·runtime 원문은 model-owner-context-d5bf5df7.json이다.

## 배포와 남은 원조건

실행 전 호스트 CPU12.9%/available38004867072bytes/pressure normal,
dashboard running_runs=0·TTS/STT ready·GPU lease held=false였다.
이 값은 호스트 전체 TTS/STT mutex나 모든 프로세스의 부재를 보증하지 않는다.

이번 배포 조회 1회에서 4a36d067은 testing/error=null, ops24행/25%/오류section0이다.
curl /api/version과 runtime_report bdea99cc-bbdc-430b-869f-ac89d033e43b는
활성116dad45203fad2c81a862309016d430eb3eae49/PID61529를 확인했다.
f519는 draining으로 새 요청이 전환됐고 이전 e6b4/PID77451는 draining이다.
native 변경 d5bf가 활성이라는 뜻은 아니다. 과거 8bf mount 오류 원인도 여전히 미확정이며
새116의 활성 관측이 그 과거 원인을 설명하지 않는다.

이후 원격 main e4899781의 별도 collector 변경을 FF 통합했다. 기존 8sourcehash는
그대로이며 제품 ML 소스 변경은 없다. runtime에 별도 d277424d/e489 예약도 관측됐다.
보안·계약 gate는 push 훅을 따른다. 같은 모델·동일 A Metal 벤치·기존 Supervisor
거부 사례를 반복하지 않았다.

실GPU owner crash, native 학습 checkpoint 재개, 전체/합산·OS memory 상한,
별도 RuntimeLease/인증 운영 UI/원UX10/legacy92와 PMS7.1/2/4/5 원조건은 미완료다.
실행 결과를 일반 품질·발음·성능 개선으로 확대하지 않는다.
