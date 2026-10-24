# 접수기 프로젝트·작업 경계 보완과 배포 병목 분석 (2026-10-24)

## 사용자 요청과 범위
따까리 배포·테스트 구조 분석을 AIP 세션에 두 번 넘긴 오분류를 지적하고 접수 하네스 보완을 요청했다. 현재 별도 기획 세션 ed811f7a-e003-4b0f-b6f6-66bdc3aa9f9a에서 처리했다. 원래 AIP 기획 5751bc1b의 두 관련 Run은 read_session 조회상 cancelled다. 접수의 실제 모델/route 이벤트는 확보하지 못했으므로 Qwen 단독 원인으로 확정하지 않는다.

## 변경
- Qwen 선택에 same_project/same_task를 필수 출력으로 추가. 모두 yes이며 작업판 ID일 때만 기존 기획을 선택한다. 선택용 작업판에서 최근 결과를 제외하고 최신 대상 우선·잘못된 분류 정정·다른 관심사·정상 지시어 후속 예시를 보강했다.
- CLI fallback parser도 두 필드를 검사한다. 누락/불일치는 새 세션을 강제하며 title dedup으로 거부한 선택을 되살리지 않는다. 저장된 desk 지침을 건드리지 않고 짧은 코드 계약을 항상 추가한다. 평가 경로와 하네스 뷰에도 동일 계약을 연결했다.
- 프롬프트 버전 harness-v3-topic-boundary, fingerprint 4483325c7c690b506fa5774e5befb046b795f3d6737ea383c126f3ab388965d4. 과거 v1/v2 정확도·지연을 v3 수치로 재사용하지 않는다.
- 배포 794447f3의 기존 실패를 좁은 테스트로 재현했다(1 failed, 6 passed in 4.25s). 최신 main에서 장면 생성 코드가 negative_prompt를 보내고 receipt의 negative hash를 요구하지만 테스트 double은 옛 계약이었다. double 인자·receipt를 맞추고 전달값 검증을 추가했다. 제품 게이트를 삭제/skip하거나 timeout을 늘리지 않았다.

## 실행 증거
수정 전 새 MockTransport 회귀 4건이 ID 오수락/결과 오염으로 실패하고, CLI 회귀 3건도 누락/불일치 무시로 실패함을 확인했다.

```text
.venv/bin/python -m pytest -q tests/test_desk.py tests/test_desk_qwen.py tests/test_desk_feedback.py tests/test_desk_qwen_live.py unit_tests/test_desk_topic_boundary.py unit_tests/test_creative_v2_lease_handoff.py --tb=short --durations=5
77 passed, 8 skipped in 19.85s

.venv/bin/python -m pytest -q tests/test_harness_view_groups.py tests/test_harness_view_api.py tests/test_harness_view_safe.py --tb=short
49 passed in 3.91s
```

8 skip은 live URL 미지정이다(기존 4 + 이번 주제 경계 4). 정책 프로토콜 테스트이지 모델의 의미 판단 정확도를 입증하지 않는다. 의미 판정은 여전히 모델에 의존한다. 추가 예시의 운영 지연도 미측정이다.

## 배포·테스트 구조 분석 근거
- 운영 API revision 9f82c9c16cd604e81ddf43180b0be2e895ec85cb, Qwen ready. 감독 로드 SHA, delivery_pipeline_enabled 실제 값, pre-push 훅 활성 상태는 미확인.
- ops_deploy_log 4a6e09a8: 4473 passed, 8 skipped, 2 xfailed, 1045.97초. selective DB reset 2822회 평균 3.4/106 테이블.
- ops_deploy_log 794447f3: 1 failed, 3327 passed, 985.34초. 위 테스트 double 결함이다.
- ADR 0066과 delivery 기반 모듈은 이미 존재한다. 소스의 opt-in worktree/ChangeSet, submit/process-once CLI, 실제 Stage validator를 확인했다. Stage 2 전체 pytest와 배포 전체 pytest가 중복될 수 있으나 운영 실제 중복 측정은 아니다. submit_change MCP/상주 integrator 연결은 찾지 못했다.
- 기존 고정 SHA 아카이브·별도 배포 DB·Run 전용 DB·선택적 테이블 초기화·세대 전환을 유지하는 방향. 다음 설계는 변경 경계 검사→자원 격리와 제한 병렬화→기존 통합기 연결→동일 후보 검증 증거의 재사용이다. 이 파이프라인 전체는 이번에 구현하지 않았다.

## 환경·미검증
Codex 중첩 worker 샌드박스에서 psutil.Process(44104).children(recursive=True)가 sysctl PermissionError로 차단됐다. 운영 모델 포트를 그 방법으로 확인하지 못했고 다른 명령으로 우회하지 않았다. uv 기본 캐시가 거부돼 허용된 /private/tmp 전용 캐시로 설치했다. 현재 작업 폴더는 최신 origin/main에서 새로 만든 독립 worktree이며 기존 공유 back의 dirty 변경은 건드리지 않았다.

사용자용 HTML 분석은 이 Run outbox에 작성했다. 브라우저 도구의 file: 프로토콜 접근 거부로 렌더·모바일 실측은 미실행. HTML 정적 검사는 8절·3개 도표·ID 유일성·미치환 슬롯 없음으로 확인했다. 전체 suite, 실모델 의미 정확도·지연 및 새 코드의 운영 동작은 배포 예약 이후 별도 증거가 필요하다.
