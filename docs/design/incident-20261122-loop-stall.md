# 2026-11-22 백엔드 이벤트 루프 정지 (15:10~17:18)

## 무슨 일이 있었나

- 8791 세대 프로세스(pid 53434)는 살아 있었지만 15:10:10 이후 2시간 8분 동안 요청에 답하지 않았다.
- 감독(deployer)의 `/api/ready` 마지막 성공은 15:10:10.8, 다음 성공은 17:18:54.6이다.
- 그동안 감독은 11초마다 `launchctl bootstrap`을 시도해 `Bootstrap failed: 5: Input/output error`만 남겼다. 라벨이 이미 올라 있어 bootstrap은 늘 실패한다.
- 알림은 없었고, 사용자가 직접 이상을 알아챘다.

## 확인한 것

- 15:21 `sample`로 떠 본 메인 스레드는 uvloop 콜백 → Python 프레임 → `os_scandir` → `__opendir2` → `__open_nocancel`에 묶여 있었다. 디렉터리 `open()` 시스템 호출 하나가 루프를 막았다.
- 15:10:13.4에 codex 오케스트레이터 Run `0697f3ae`가 마지막 메시지를 남겼다. 이 Run의 `finished_at`은 17:18:50으로 루프가 풀린 시각과 같다. 이 Run의 종료 경로에서 루프가 막혔을 가능성이 높다.
- 같은 시간대에 점검용 셸 명령(`mount; ls /Volumes; lsof -p 53434`)도 15:21:31부터 17:18:52까지 멈췄다. 따까리 코드만이 아니라 시스템 쪽 파일시스템 호출이 함께 막혔다.
- 이 Run의 outbox는 비어 있었고, codex rollout glob은 지금 재면 0초다. 사후에는 재현되지 않는다.

## 확인하지 못한 것

- 어느 경로의 `open()`이 막혔는지는 모른다. 그 시점의 Python 스택이 없고, 커널 로그에도 디스크·NFS 오류가 남지 않았다.
- 외장 볼륨(`/Volumes/Backup`의 outbox·artifacts·kb 링크), OrbStack NFS, File Provider(Google Drive) 중 어느 것이 막혔는지 구분할 증거가 없다.

## 고친 것

1. `loop_stall.py`: 루프 밖 감시 스레드가 1초 심장박동을 본다. 30초(`loop_stall_dump_seconds`) 넘게 멈추면 모든 스레드의 Python 스택을 로그에 남기고, 계속 멈춰 있으면 10분마다 다시 남긴다. 다음 정지에서는 막힌 호출과 경로를 바로 알 수 있다.
2. 감독: `/api/ready`가 실패해도 launchd 라벨에 살아 있는 pid가 있으면 bootstrap을 하지 않는다. 연속 실패가 300초(`generation_hang_restart_seconds`)를 넘으면 푸시(`push.server_hang`)로 한 번 알리고 `launchctl kickstart -k`로 재시작한다. 값을 0으로 두면 알림만 한다. pid가 바뀌거나 한 번이라도 응답하면 시간을 처음부터 다시 잰다.
3. Run 종료 경로의 동기 파일 호출 두 곳을 스레드로 옮겼다. outbox 순회(`_outbox_candidates`)와 codex 컨텍스트 측정(`context_usage.measure`)이다. 이번 원인으로 확정한 것은 아니고, 루프 위에서 외장 볼륨을 도는 호출을 줄이려는 것이다.

## 남은 것

- 자동 재시작은 실행 중인 Run을 끊는다. 재시작 뒤 정리는 기존 Run 보상(`compensate_runs`)에 맡긴다.
- 루프 위에 남은 동기 디렉터리 호출(`api/transfers.py` 폴더 목록, `harness_view` 등)은 다음 정지 스택을 보고 우선순위를 정한다.
