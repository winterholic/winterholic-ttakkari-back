# 백업·복원 runbook (SEC2-OPS-01)

## 파일

- `scripts/backup_db.sh`: pg_dump(-Fc) + 테이블별 행 수 manifest. 운영 DB에는 쓰지 않는다(읽기 전용).
- `scripts/restore_rehearsal.sh`: 격리 DB에 복원하고 manifest와 행 수를 대조한다.
- `deploy/com.apple.ttakkari.backup.plist.example`: 매일 03:17 실행 launchd 예시.
- `unit_tests/test_backup_restore.py`: 재현·회귀 테스트.

## 지금 바로 하기

```sh
cd back
./scripts/backup_db.sh                                   # 백업 1회
./scripts/restore_rehearsal.sh --manifest "$(ls -t ~/.ttakkari-backups/*.manifest.json | head -1)"
```

최근 성공 시각 조회: `ls -t ~/.ttakkari-backups/*.manifest.json | head -1`

## 스케줄 등록 (Mac Studio 본체에서)

```sh
sed 's/BACKUP_USER/winterholic/' deploy/com.apple.ttakkari.backup.plist.example \
  > ~/Library/LaunchAgents/com.apple.ttakkari.backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apple.ttakkari.backup.plist
launchctl print gui/$(id -u)/com.apple.ttakkari.backup | grep -E 'state|last exit'
```

## 실DB 리허설 (Mac Studio 본체에서, 샌드박스가 아닌 셸)

```sh
TTAKKARI_LIVE_REHEARSAL=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  unit_tests/test_backup_restore.py::test_live_rehearsal_against_postgres
```

## Time Machine (사용자 조치 필요)

`tmutil latestbackup`이 대상 마운트 실패로 백업이 안 되고 있다.
(Time Machine 대상 디스크·NAS를 사용자가 직접 확인해야 한다. 디스크 설정은 건드리지 않았다.)
복원 리허설과 별개로 오프머신 복구 지점이 없으므로, Time Machine 복구 전에는
`~/.ttakkari-backups`를 외장 디스크에 복사해 두는 것을 권장한다.

## 보관과 한계

- 보존 기간 기본 14일(`--retention-days`로 변경).
- 백업 위치 기본값 `~/.ttakkari-backups`는 에이전트 쓰기 루트 밖이다.
  같은 macOS 사용자라는 한계(위협 모델 6.1절)상 권한 분리는 Time Machine으로 보완한다.
- 최근 성공 시각의 운영 상태 노출(API)은 후속 작업으로 남긴다.
