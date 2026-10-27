# OpenCode·Copilot 실행기 마무리: 프로브 보고 등록과 오류 진단 스크럽

멈춘 탐색 세션(worker_routing/capabilities를 `rg`로 3시간째 조회) 대신 새 세션에서
남은 등록 누락을 직접 메웠다. 멈춘 세션의 정확한 지시서는 이 세션에서 조회할
수단이 없어(MCP 미바인딩), 코드 감사를 기준으로 미완 항목만 구현했다.

## 범위와 판정

- `engine="opencode"`·`engine="copilot"` 실행·라우팅·계정·quota·seatbelt는 이미
  main에 있다. auto ROUTES 편입은 ADR 0071/0072의 불변 조건대로 두었다.
  실제 OAuth·모델 응답·재개·MCP 호출의 운영 수용 검증은 여전히 미완료이며
  무인으로는 할 수 없다(브라우저 승인 필요).
- 프론트(opencode-front, fix/copilot-usage-ui 브랜치)는 main 미병합 상태로
  남았다. front main 체크아웃에 타 작업의 미커밋 변경 29개가 있어 손대지 않았다.

## 변경

- `runs/capabilities.py` ENGINES에 `opencode`, `copilot` 추가. 두 엔진 세션도
  환경 프로브 challenge 발급·보고가 되고, 라우팅 영수증·프롬프트 안내에
  측정값이 실린다. 기존 길이 상한(1400/2000)은 그대로 둔다.
- 미병합 브랜치 `fix/opencode-error-diagnostics`(ddf3ff11)를 현재 main에
  재적용: `opencode run`에 `--print-logs --log-level ERROR`(설치 CLI help로
  실측 확인), `finalize_process`의 stderr를 `scrub`하고 UnknownError의
  "Unexpected server error"에는 ERROR stderr 원인을 3000자로 붙인다.
- `copilot.py` `finalize_process`도 같은 `scrub`을 적용(자격증명 노출 방지).
- 테스트: 어댑터 6개 추가·수정, capabilities 신규 엔진 왕복 2개 추가,
  미측정 고정 어서션 갱신. 원본 ddf3ff11의 자명한 토큰 어서션 1개는 실제
  토큰값으로 강화했다.

## 실행 검증

- `pytest unit_tests/test_opencode_adapter.py unit_tests/test_copilot_adapter.py`
  → 22 passed. 기존 opencode/copilot 9파일 묶음은 변경 전 92 passed.
- `tests/test_execution_capabilities.py`는 이 샌드박스에서 실행 불가다.
  conftest가 격리 PostgreSQL을 띄우는데 loopback이 막혀 39건 전부 환경
  오류다(코드 실패 아님). 대신 DB 없는 동등 검증 스크립트로 record/
  challenge/report 왕복·prompt_note 내용·길이 상한·unknown 거부를 확인했다.
- `unit_tests/` 전체: 변경과 무관한 `test_backup_ops_remaining` 1건이
  변경 전후 동일하게 실패(사전 결함). 나머지는 통과.
- 설치 CLI 대조: copilot 1.0.103의 `-p/--output-format/--allow-all-tools/
  --no-ask-user/--no-auto-update/--stream/--model/--session-id/-C/
  --reasoning-effort/--additional-mcp-config` 전부 존재. opencode run의
  `--print-logs/--log-level/--pure/--session/--model/--format/--dir/
  --variant` 전부 존재(격리 XDG로 help 실측).
