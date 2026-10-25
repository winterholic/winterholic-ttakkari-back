# 백업·복원 runbook (SEC2-OPS-01)

## 파일

- `scripts/backup_db.sh`: pg_dump(-Fc) + 테이블별 행 수 manifest. 운영 DB에는 쓰지 않는다(읽기 전용).
  `--passphrase-file`(또는 `BACKUP_PASSPHRASE_FILE`)을 주면 openssl enc -aes-256-cbc -pbkdf2로
  암호화하고 평문 덤프는 디스크에서 지운다. 성공 시 `latest.json`을 갱신한다.
- `scripts/restore_rehearsal.sh`: 격리 DB에 복원하고 manifest와 행 수를 대조한다.
  암호화 산출물이면 같은 암호 파일로 임시 위치에 복호화한 뒤 대조하고 지운다.
- `scripts/snapshot_data.sh`: 데이터 디렉터리 tar.gz 스냅샷 + 파일별 sha256 manifest.
  `--verify`로 격리 위치에 풀어 대조한다. 암호화·`latest-data.json` 갱신을 지원한다.
  심볼릭 링크·특수 파일과 `*.sock`·`*.pid`는 제외하며, manifest는 아카이브에 실제 담긴 파일의 바이트로 계산한다.
  검증 뒤 정리와 이전 잔여 파일 정리는 링크 자체만 지우며 링크 대상의 내용·권한을 바꾸지 않는다.
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

## 에이전트 호스트 운영 작업 (사용자 셸 불필요)

위 "지금 바로 하기"의 셸 명령은 수동 대안이다. 배포된 백엔드는 같은 스크립트를 고정 argv 로 재사용하는
승인된 작업 API 를 제공한다. 에이전트는 MCP 도구 `host_operation_start` / `host_operation_status` /
`host_operation_cancel` 로 호출하고, 사용자는 아무것도 직접 실행하지 않는다.

| kind | 하는 일 | 쓰기 |
| --- | --- | --- |
| `runtime_report` | 활성/실행기 세대, 세대별 lease·프로세스 그룹 수, 최근 배포 예약 | 없음 |
| `performance_baseline` | DB 왕복 20회·이벤트 루프 지연 20회·호스트 압박(수 초, 합성) | 없음 |
| `backup_status` | 마지막 DB 백업·스냅샷 시각/개수/해시, 키 출처 | 없음 |
| `backup_db` | 운영 DB 읽기 전용 `pg_dump` 새 사본을 암호화해 백업 디렉터리에 저장 | 새 파일만 |
| `data_snapshot` | `data_dir` 만 암호화 스냅샷(설정·계정·키 제외) | 새 파일만 |
| `restore_rehearsal` | 최근 DB 백업을 `ttakkari_restore_rehearsal_<12hex>` 임시 DB 에 복원, 행 수 대조 후 삭제 | 임시 DB 만 |
| `snapshot_verify` | 최근 스냅샷을 백업 디렉터리 `.verify/` 임시 위치에 풀어 해시 대조 후 삭제 | 임시 폴더만 |

- 호출자가 고르는 값은 `kind` 하나다. 경로·DB URL·대상 DB 이름·셸 문자열은 받지 않고(`extra=forbid`), 모두 설정과
  manifest 에서 백엔드가 정한다. 최근 백업 manifest 는 백업 디렉터리 안의 정해진 파일명·비심볼릭 파일만 허용한다.
- 복원은 로컬 PostgreSQL 에서만, 접두사+작업 id 로 만든 새 DB 에만 한다. 운영 DB 이름과 같거나 접두사가 다르면 거부한다.
  운영 DB·원본 파일은 덮어쓰거나 지우지 않는다. 러너가 끝난 뒤 남은 격리 DB 가 있으면 규칙에 맞는 이름만 지운다.
- 개인 세션만 호출할 수 있다(work 세션 403). 무거운 작업은 한 번에 하나이고, 같은 kind 를 다시 부르면 진행 중 작업을
  돌려준다(`deduplicated`). 다른 kind 는 409. 시간 제한: backup_db 15분, 나머지 30분. 러너는 `nice 10` 으로 돈다.
- 분리된 러너 프로세스가 돌고 상태는 `~/.ttakkari/operations/jobs/*.json` 에 남아 API 재시작·세대 교체를 견딘다.
  러너가 사라지면 기록된 자식 PID·생성 시각·명령행·프로세스 그룹을 모두 확인한 뒤에만 그룹을 정리한다. 신원 확인·권한·그룹 종료를
  확인할 수 없으면 `cleanup_pending` 으로 남아 무거운 작업 슬롯을 유지한다. 이는 운영자가 `/api/agent/operations/{id}` 상태를 다시 조회해도
  증거가 확보되기 전에는 실패 종결로 바뀌지 않는다. 오류 문구의 pid·pgid 를 호스트에서 직접 확인해(`ps -g <pgid>`) 그룹이 실제로 없음이 분명할 때만
  운영자가 해당 작업 기록(`~/.ttakkari/operations/jobs/<id>.json`)을 정리한다. 앱은 신원 결속이 안 된 그룹에는 신호를 보내지 않는다. 취소·시간 초과도 러너가 그룹 전체(리더만이 아니라 손자까지)의 소멸을 확인한 뒤에만 `cancelled`/`timeout` 으로 남기며, 확인하지 못하면 `cleanup_pending` 으로 슬롯을 유지한다.
  자식을 띄우는 순간 러너가 죽어 신원이 기록되지 않은 경우에는 자식이 fork 직후 스스로 적은 pid 파일(`jobs/<id>.child`)과 기록해 둔 예정 명령 해시,
  시작 시각으로 신원을 복원해 같은 검증을 거친 뒤 정리한다. fork 직후 pid 파일을 쓰기 전 창에서는 유예(기본 10초) 동안 `cleanup_pending`을 유지하고,
  유예가 지나도 파일이 없으면 자식이 띄워지지 않은 것이므로 `runner_lost` 로 종결한다. pid 파일 내용이 비정상(-1 등)이어도 예외 없이 `cleanup_pending`으로 남긴다.
  복원이 안 되면(`unrecorded`) 신호를 보내지 않고 `cleanup_pending` 으로 남으며 오류에 job id·kind·runner pid/시작 시각이 들어 있다. 이때는
  `ps -axo pid,pgid,ppid,lstart,command | grep -E 'scripts/'` 로 러너 시작 시각 이후에 뜬 `scripts/` 명령(백업·스냅샷·복원 스크립트)을 찾는다.
  부모가 1(launchd)이고 오류의 runner 시각 직후에 시작된 프로세스가 그 고아다. 그룹(`pgid`)을 확인해 종료하면 다음 조회에서 자동으로 `failed` 로 풀리고,
  아무것도 없음이 분명하면 `jobs/<id>.json` 을 정리한다.
  종료 감사는 DB 커밋 뒤 `finish_audited` 표식을 기록한다. 감사 커밋에 실패하면 terminal 상태와 `audit_pending`/`audit_error` 를 보존하고
  다음 상태·목록 조회에서 재시도한다. 요청·취소·종료는 감사 로그(`operation.request|cancel|finish`)에 남는다.
- 결과에는 개수·해시·시각·대상 identity 만 담는다. 출력 꼬리는 `scrub` 마스킹 후 경로를 `<home>` 등으로 치환해 1500자만 보존한다.
- 백업은 항상 암호화한다. 키는 `TTAKKARI_BACKUP_PASSPHRASE_FILE`(0600 파일 경로, 설정으로 주입)이 있으면 그것을 쓰고, 없으면
  첫 `backup_db`/`data_snapshot` 때 앱이 `~/.ttakkari/backup.key` 를 임시파일+rename 방식으로 0600 원자 생성한다. 키 내용은 읽어서 출력하지 않으며,
  manifest 와 상태 파일에는 느린 PBKDF2 로 파생한 키 지문(`key_fingerprint`, 16자리 hex, 내용 아님)만 기록해 일치 여부를 대조한다.
  **경고**: 키 파일을 분실하면 암호화된 기존 DB 덤프와 데이터 스냅샷을 절대 복호화할 수 없다(복원 영구 불가). 따라서 보관 위치인
  `~/.ttakkari/backup.key`(또는 지정 경로)의 키 파일은 외장 디스크나 안전한 오프머신 백업 저장소에 반드시 함께 복사·보관해야 한다.
  한계: 기본 상태에서는 키와 백업이 같은 호스트에 있다. 호스트 밖 보관(외장 디스크·Time Machine)은 아래 "Time Machine" 절을 따른다.
- 백업 디렉터리는 `backup_status.backup_dir()`(환경 변수 `BACKUP_DIR`, 기본 `~/.ttakkari-backups`)이며 `GET /api/system/backup-status` 가 읽는
  곳과 같다. 보존 기간은 30일(기존 14일 기본 대신 더 오래 둔다).

배포 후 호출 순서: `backup_status` → `runtime_report` → `performance_baseline` → `backup_db` → (완료 확인) `restore_rehearsal` →
`data_snapshot` → `snapshot_verify`. 모든 호출은 `host_operation_status` 로 결과를 확인한다.

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
