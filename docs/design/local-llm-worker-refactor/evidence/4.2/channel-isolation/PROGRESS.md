# 4.2 채널·격리 중간 checkpoint (최종 CPU 결과는 summary.json/§11)

2026-10-23. 시작 852d561 → git pull --ff-only origin main으로 ba359c8까지 FF. 원본 dirty checkout은 편집하지 않음.

사용자 새 범위: 공격 페이로드 추가 금지. 전용 framed pipe / sandbox-exec 읽기 allowlist / 정책 축소와 정상해 호환성 / 구조 불변식 회귀 / main push / PMS doing 유지.

구현 중:
- channel.py: os.pipe 전용 관측채널, LLO1+길이 header, 한 frame 정확검사, identity envelope와 trusted writer의 fd+guard duplicate fstat 검사. stdout/stderr는 진단만, bounded selector drain, timeout/outputlimit/process group cleanup, nonzero fail.
- runner.py: spec/probe는 stdin으로만 전달(디스크 spec 파일 없음), pinned bootstrap. Python은 venv 링크가 아니라 real runtime /Users/winterholic/.local/share/uv/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13을 실행.
- sandbox.py: default deny, system+runtime+후보tree/home/tmp/데이터디렉터리만 읽기 허용, 네트워크/fork 거부. broad repo/.venv/home allow 없음. dev fallback은 sandbox_unavailable, holdout Task.split 또는 id -H는 실행 전 INFRA_ISOLATION_UNAVAILABLE.
- 이 Codex Run에서 /usr/bin/sandbox-exec -p ... /usr/bin/true → exit71 sandbox_apply: Operation not permitted 실측. 동일 거부 재시도/우회 없음. 이후 테스트는 LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE에 측정 이유를 전달. OS deny 테스트는 skip으로 명확히 기록. Claude 호스트 측 보고를 독립 실측으로 간주하지 않음.
- 원본 checkout 실제존재 /Users/winterholic/development/projects/winterholic-ttakkari/back. 그 checkout의 bench/local_llm_eval/evaluator.py는 없음. 앞 FileNotFoundError는 deny 증거가 아니라 없는 경로였음. 실제 있는 원본 pyproject.toml을 deny target으로 바꿈. protected_sources에서 파일 존재를 먼저 확인.
- integrity policy: 계산/시간/경로 stdlib 확장, native AST 동일집합 사용, 보호된 getattr/hasattr, type 본래 identity + 반환 타입 속성 가드, dunder definitions/__all__ 허용. 기존 guard binding/명백한 제어변조 거부 유지. 애매한 format/dunder hook은 flag.
- DBG는 기존 함수와 문장 보존을 유지하되 무해한 prelude/helper scaffolding 추가 허용. 하드 규칙을 완화해 reference+harmless prefix를 살림.

확인한 실행: channel 구조 14 passed/1 skipped(실제 OS deny), prefix 14종×8과제=112 passed(30.72s). full pytest와 selfcheck가 현재 실행 중(원출력은 같은 폴더).

남은 일: 전체 테스트/selfcheck 결과 → 발견된 실제 실패만 수정 → sandbox readiness 및 deny CLI/문서·manifest 증거 갱신 → commit/rebase → pre-push ruff/계약/평가기 전체(우회 금지) → main FF push → PMS 설명·진행률 갱신/doing 유지 → 현재 Run outbox 9b2222e2-1cd1-4e7f-b5e7-80e4473c6fd2에 결과·원출력 공유.

주장 한계: binary framing과 child의 fd identity 체크는 암호학적 인증이 아니며 같은 interpreter의 임의 메모리 변조 완전 방어 증명이 아님. 기대값/정답은 부모에만 있고 probe 메모리 노출을 불가능하다고 주장하지 않는다. 개발 정답은 저장소에 공개되어 위조방어가 약함. 강한 보장의 선행조건은 봉인 holdout+검증된 OS 격리. 현재 엔진에서 OS deny는 미검증. holdout/정답봉인·실모델·서버 실행 금지 및 미완 유지.

후속 CPU 결과: pytest 280 passed, 1 skipped(126.18s), selfcheck problems=0, 참조18/18 PASS·음성366/366 기대사유 거부. selfcheck에도 isolation flag를 보존하는 red→green 회귀 추가. 위 중간 미완료/실패는 당시 기록이다. 실제 OS deny는 계속 미검증이며 summary.json의 strong_integrity_certified=false가 정본이다. commit/push/PMS는 최종 보고 참조.
