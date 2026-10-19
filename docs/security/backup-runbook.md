# 백업·복원 runbook (SEC2-OPS-01)

## 파일

- `scripts/backup_db.sh`: pg_dump(-Fc) + 테이블별 행 수 manifest. 운영 DB에는 쓰지 않는다(읽기 전용).
  `--passphrase-file`(또는 `BACKUP_PASSPHRASE_FILE`)을 주면 openssl enc -aes-256-cbc -pbkdf2로
  암호화하고 평문 덤프는 디스크에서 지운다. 성공 시 `latest.json`을 갱신한다.
- `scripts/restore_rehearsal.sh`: 격리 DB에 복원하고 manifest와 행 수를 대조한다.
  암호화 산출물이면 같은 암호 파일로 임시 위치에 복호화한 뒤 대조하고 지운다.
- `scripts/snapshot_data.sh`: 데이터 디렉터리 tar.gz 스냅샷 + 파일별 sha256 manifest.
  `--verify`로 격리 위치에 풀어 대조한다. 암호화·`latest-data.json` 갱신을 지원한다.
- `deploy/com.apple.ttakkari.backup.plist.example`: 매일 03:17 실행 launchd 예시.
- `deploy/com.apple.ttakkari.data-snapshot.plist.example`: 매주 일요일 03:47 실행 launchd 예시.
- `unit_tests/test_backup_restore.py`: 기존 재현·회귀 테스트(비암호화 대조군).
- `unit_tests/test_backup_ops_remaining.py`: 암호화·스냅샷·상태·스케줄 테스트.
- `src/ttakkari/backup_status.py` + `GET /api/system/backup-status`(인증 필요):
  마지막 DB 백업·데이터 스냅샷 성공 시각 조회. 비밀은 안 담는다(개수·해시만).

## 암호 설정 (Mac Studio 본체에서 1회)

```sh
openssl rand -base64 32 > ~/.ttakkari-backup.key && chmod 600 ~/.ttakkari-backup.key
```

암호 파일은 저장소에 커밋하지 않는다. 권한이 0600이 아니면 스크립트가 거부한다.
분실하면 기존 암호화 산출물을 복원할 수 없으니 외장 디스크 복사본과 함께 보관한다.

## 지금 바로 하기

```sh
cd back
./scripts/backup_db.sh --passphrase-file ~/.ttakkari-backup.key   # 암호화 백업 1회
./scripts/restore_rehearsal.sh --passphrase-file ~/.ttakkari-backup.key \
  --manifest "$(ls -t ~/.ttakkari-backups/*.manifest.json | head -1)"
```

암호 없이 도 기존처럼 된다(평문 보관, 기존 테스트 대조군).

## 데이터 스냅샷 (격리 위치에서 먼저 시험)

```sh
cd back
mkdir -p /tmp/snap-test-src && echo hello > /tmp/snap-test-src/a.txt
./scripts/snapshot_data.sh --source /tmp/snap-test-src --dir /tmp/snap-test-snaps
./scripts/snapshot_data.sh --verify "$(ls -t /tmp/snap-test-snaps/*.manifest.json | head -1)" \
  --target /tmp/snap-test-restored
```

운영 데이터(예: `~/.ttakkari/data`)는 격리 시험이 통과한 뒤에 원본으로 지정한다.
스케줄 예시는 매주 일요일 03:47이다.

최근 성공 시각 조회:

```sh
cat ~/.ttakkari-backups/latest.json        # DB 백업
cat ~/.ttakkari-backups/latest-data.json   # 데이터 스냅샷
curl -s http://127.0.0.1:8787/api/system/backup-status -H "Authorization: Bearer $TOKEN"
```

## 스케줄 등록 (Mac Studio 본체에서)

```sh
sed 's/BACKUP_USER/winterholic/' deploy/com.apple.ttakkari.backup.plist.example \
  > ~/Library/LaunchAgents/com.apple.ttakkari.backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apple.ttakkari.backup.plist
launchctl print gui/$(id -u)/com.apple.ttakkari.backup | grep -E 'state|last exit'

sed 's/BACKUP_USER/winterholic/' deploy/com.apple.ttakkari.data-snapshot.plist.example \
  > ~/Library/LaunchAgents/com.apple.ttakkari.data-snapshot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apple.ttakkari.data-snapshot.plist
launchctl print gui/$(id -u)/com.apple.ttakkari.data-snapshot | grep -E 'state|last exit'
```

`launchctl bootstrap`은 호스트 설정을 바꾸므로 샌드박스 Run에서는 실행하지 않는다.
등록 해제는 `launchctl bootout gui/$(id -u)/com.apple.ttakkari.backup` (스냅샷도 동일 형식).

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
암호화 산출물은 암호 파일(`~/.ttakkari-backup.key`)도 함께 복사해야 복원할 수 있다.

## 보관과 한계

- 보존 기간 기본 14일(`--retention-days`로 변경).
- 백업 위치 기본값 `~/.ttakkari-backups`는 에이전트 쓰기 루트 밖이다.
  같은 macOS 사용자라는 한계(위협 모델 6.1절)상 권한 분리는 Time Machine으로 보완한다.
  암호화는 외장 복사본 분실·도난 시 평문 노출을 막는다.
