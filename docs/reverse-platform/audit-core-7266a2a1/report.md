# Core 교정 독립 감사 — 7266a2a1 (2026-11-07)

## 판정

**이 core 범위에서는 이전 핵심 결함의 지정 반례가 해소된 것을 확인했다. 전체 감사 상태나 제품 릴리스는 통과로 선언하지 않는다.** 새 고정 snapshot에서 독립 verifier의 자체 비교·필수 검사, verifier mismatch repair 및 한도, callback 중복/지연, 취소 후 retry, 실제 임시 DB detail 응답을 검증했다.

## 고정 기준과 격리

- backend HEAD: `7266a2a19332935a6e32d10108a7e021956ef910` (`fix(reverse): enforce independent verification evidence`), parent `96becddb1170e6750560f2d9bd7f7f97bcbe2bc0`.
- 새 detached worktree: `back-reverse-audit-core-20261107`. `git worktree add --detach`로 commit에서 만들었으며 시작 시 HEAD가 위 SHA이고 tracked/untracked 변경이 없음을 확인했다. live `back-reverse-evidence-20261107` dirty diff는 가져오지 않았다.
- 이전 독립 감사 보고서 및 snapshot은 덮어쓰지 않았다. 산출물은 이 하위 경로의 probe와 본 보고서다.
- Probe는 `tests/conftest.py`의 프로세스별 임시 PostgreSQL 및 테스트 설정을 플러그인으로 불러오고 `manager.dispatch`를 no-op으로 바꿔 외부 agent 실행을 막는다. 운영 DB·서비스 재시작·배포·비밀 파일 읽기는 없었다.
- 이전 snapshot의 24개 결과와 취약 동작을 assert해 통과시키는 probe는 재사용하지 않았다. 기존 제품 소스 및 구현자 테스트도 변경하지 않았다.

## 실행 증거

독립 probe:
[test_core_audit.py](probes/test_core_audit.py)

```text
$ PYTHONPATH=src:tests /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q docs/reverse-platform/audit-core-7266a2a1/probes/test_core_audit.py
.....                                                                    [100%]
5 passed in 2.30s
```

## 판정 세부

| 기준 | 결과 | 관측된 상태 |
|---|---|---|
| verifier 자체 비교 0건 | **통과** | verifier는 `partial`로 남고 `independent_verification=failed`, verifier 직접 비교 `unverified/count=0`으로 보존됨. 구현자의 비교가 verifier 결과를 대신하지 않음. |
| verifier 비교 mismatch | **통과** | 직접 재산출된 `mismatch`에서 완료되지 않음. 제한이 남아 있으면 같은 구현 session에 `repair` Run을 만들고 시도 횟수를 증가시킴. |
| verifier 필수 검사 실패 | **통과** | 성공 필수 검사와 실패 필수 검사를 함께 둔 verifier 보고는 `partial`; 실행 준비/검증 판정이 실패로 남음. |
| verifier mismatch repair와 재검증 | **통과** | repair Run이 원래 구현 session을 유지함. repair 완료 뒤 새 verifier session/Run이 생성되고, repair 전 구현 회차는 history에 보존됨. 오래된 verifier callback은 무시됨. |
| repair 한도 소진 | **통과** | 1회 한도 소진 뒤 다음 verifier mismatch는 `partial`로 종료. 마지막 verifier의 mismatch 비교·Run ID·실패 상태가 latest에 남고 추가 repair는 생성되지 않음. |
| 성공 verifier callback 중복 | **통과** | 동일 Run callback 재호출 시 Run 수와 history 길이가 늘지 않고 latest Run ID가 유지됨. |
| 취소·retry·늦은 callback | **통과** | 취소 verifier는 `partial/cancelled`. retry는 새 queued 구현 Run을 만들고 구현 완료 history를 보존하며 verifier 연결을 비움. 이전 verifier callback을 retry 뒤 다시 불러도 새 상태를 덮지 않음. |
| public detail 계약 | **통과** | 실제 ASGI API와 임시 DB 응답에서 `result.latest == latest`, `result.history == history`; `result_state`의 source, 직접 비교 count, 독립 검증 상태가 완료 snapshot과 일치함을 확인함. retry 후 빈 latest 상태도 API에서 동일하게 표현됨. |

## 남은 범위와 제한

- 이 결과는 지정한 core 반례를 고정 snapshot에서 실행한 것이다. 전체 기능 정확도나 전체 시스템 통과를 뜻하지 않는다.
- 새 웹/UI 변경, 프론트 consumer 호환성, 실제 플랫폼 agent가 수행하는 CLI/API/Web E2E, 배포 revision 및 운영 동작은 **미검증**이다.
- 최종 운영 감사는 별도 후속 카드로 남는다. 기존 감사에서 발견된 frontend/backend detail 소비 불일치가 새 UI revision에서 해소됐는지도 여기서는 판정하지 않았다.
- 기획이 확인한 `91 passed`는 배경 기록이다. 이 감사의 증거는 위 독립 probe의 `5 passed`뿐이다.

## 재실행

저장소 root가 이 worktree일 때:

```bash
PYTHONPATH=src:tests /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q docs/reverse-platform/audit-core-7266a2a1/probes/test_core_audit.py
git diff --check
```
