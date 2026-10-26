# 운영 배포 Game Studio 설치 실패: 관측 보강

2026-10-26 리드 Codex가 Research Lab의 미완료 운영 인수를 이어갔다.
전체 목표는 active다. 이 변경은 설치 장애의 원인 교정이 아니라, 실패했던
배포 환경에서 다음에 필요한 원본 로그를 확보하기 위한 테스트 진단 보강이다.

## 확인한 배포와 환경

- `backend_deploy_status(d9103cfa-47a8-49fc-afd7-a4987629aef5)` 1회:
  `5e7618bf9bc602a0e950d7e12b3d5f65da194830`, failed, skip_tests=false.
- `ops_deploy_log(d9103cfa, summary 및 range offset25/60)`:
  `tests/test_e2e_game_studio.py:78`, 첫 API 빌드의
  `의존성 설치 실패(exit 127)`. `1 failed, 1322 passed, 10 skipped`, 193.08초.
- `runtime_report`에서 선행 `7b3bac82`도 failed임을 처음 확인하고,
  해당 ops summary를 읽었다. 후보151195b9의 다른 테스트
  `tests/test_game_browser_smoke.py:56`도 같은 설치 exit127이었다.
  `1 failed, 1407 passed, 10 skipped`, 322.80초.
- 실패 환경은 **배포 컨트롤러가 실행한 릴리스 pytest의 격리 컨테이너
  설치 단계**다. stdout/stderr를 원래 API의 log_tail에 저장하지만,
  테스트 단언은 error만 표시해 ops 배포 로그에서 상세가 빠졌다.
  npm 실행 파일·이미지·네트워크 등 세부 원인은 현재 증거로 확정하지 않았다.
  호스트 전체 또는 Research 어댑터의 실행 불가로 확대하지 않는다.

## 별도 Codex 명령 환경의 좁은 실행

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_game_adapter.py::test_real_phaser_template_builds_in_isolation -q --timeout=480 --tb=short
1 passed in 9.93s
$ PYTHONPATH=src .venv/bin/python -P /private/tmp/research_game_install_probe.py
BUILD_DIAGNOSTIC succeeded None
install exit=0 / check exit=0 / build exit=0
1 passed in 8.87s
```

두 번째 실행은 G14의 실제 기록 소스v1을 scaffold에 복사·커밋하고,
실제 ASGI API로 등록/잠금/빌드/상태 조회를 실행했다. 주입된 테스트 DB와
`current_database/current_user`를 대조하고 운영 DB를 거부했다.
원본 진단 script는 [repro-game-recorded-build.py](repro-game-recorded-build.py),
실행 출력은 [game-recorded-build-probe.txt](game-recorded-build-probe.txt)에 있다.
pytest plugin으로 G14 본체를 **첫 API 빌드만 수행하는 함수**로 대체했다.
브라우저와 게임플레이를 실행하지 않았으므로 전체 G14 PASS가 아니다.
또한 컨트롤러의 전체 선행 테스트 순서·환경을 재현한 것이 아니다.
실패 원인의 부재 증거나 재현 성공으로 사용하지 않는다.

## 최소 변경과 합성 반례

실패했던 G14·G13 테스트 두 파일에서 error와 함께 기존 log_tail을 표시한다.
G14 롤백 빌드도 같은 방식으로 보강했다. 성공 상태·lock·복구 원본·번들 일치
조건은 유지한다. 게임 제품 코드, 설치 명령, 격리 정책, Research 제품 코드,
DB/schema, 배포 통과 기준과 재시도 정책은 변경하지 않았다.

```text
$ .venv/bin/python -P docs/research-lab/evidence/repro-game-install-diagnostic.py
교정 전 exit1: AssertionError: G14 hides install stderr: synthetic failure
교정 후 exit0: failed build: error and original log are visible
successful locked build: accepted / unverified lock: rejected
G13 failed build: original log is visible / G13 successful build: accepted
$ .venv/bin/ruff check tests/test_e2e_game_studio.py tests/test_game_browser_smoke.py docs/research-lab/evidence/repro-game-install-diagnostic.py docs/research-lab/evidence/repro-game-recorded-build.py
All checks passed!
$ git diff --check
exit0
```

검사기는 저장소의 실제 G14 build 함수와 G13 status 단언을 AST로 꺼내
합성 API 실패 응답을 넣는다. 설치 장애나 실제 브라우저 검사가 아니다.
다음 후보 검사는 새로운 로그를 확보할 수 있어 기존 SHA 재예약과 구분된다.
같은 suite의 수동 전체 재실행이나 무근거 자동 재시도는 추가하지 않았다.

## 운영 관측과 남은 요구

`curl -fsS http://127.0.0.1:8787/api/version`은
fcd3dd161406a12e32ed9a9c9a65fb5e58c207b0/PID87194를 반환했다.
`host_gpu_status`의 마지막 request는 qwen3.5-4b-q4_k_m/q8_0 Key/f16 Value,
started_at1792992954.600136/stopped_at1792992958.460334로 기존 A treatment와
일치했다. stopped/running=false/exit0. 아직 이전 parser여서 장치 초기화8줄뿐이다.
벤치를 재실행하지 않았다. 새 parser 활성 후 같은 launch인지 다시 대조해
보존 placement 로그를 읽어야 한다.

`host_operation_start(runtime_report)` 8fd8a843은 현재 활성 fcd3/로컬 API,
전체 RuntimeLease1개/실행 Run1개를 반환했다. 이것은 세대 전체의
집계이며 연구별 lease 인수 증거가 아니다. 코드상 연구 batch는
`generation.enter_accepted_work('research')`를 쓰며 이는 메모리의 accepted-work
추적이다. 에이전트 Run의 DB RuntimeLease와 구분한다. 연구별 장시간 복구·소유권은
여전히 미검증이며 별도 완료로 승격하지 않는다.

설정된 ttakkari_browser로 https://ttakkari.winterholic.net/research를 열었다.
로그인 화면 `/login?next=%2Fresearch`, 제목 Winterholic Ttakkari,
비밀번호 입력 및 Mac Studio 응답 중을 확인했다. 격리 프로필에는 인증 세션이
없어 내부 운영 A/기록 화면 스모크는 미실행이다. 비밀 조회·인증 우회 없이
브라우저를 닫았다. 기존 프론트 Production 성공은 재배포하지 않는다.

기존 A 2Run·BestResult·575필드 보존·B/C/D 및 UI 인수는 기존 증거를 재사용한다.
R4/7/12~16, PMS6/7/8의 미완료 원조건을 유지한다. 새 누적 후보를 commit/push하고
full SHA를 기존 컨트롤러에 예약한다. 다음 Run은 예약 결과 1회→실패하면 원본
설치 log_tail로 원인 교정, 활성화됐으면 version/동일launch placement→운영 인수로
이어간다. 예약 대기 폴링은 하지 않는다.
