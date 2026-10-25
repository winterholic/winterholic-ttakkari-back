"""호스트 운영 작업(백업·격리 복원·소유권 보고·기준 성능). 에이전트가 승인된 종류만 부른다.

임의 셸·경로·DB URL 을 받지 않는다. 호출자가 고르는 것은 `kind` 하나이고, 인자·경로·대상 DB 는
이 패키지가 설정에서 정한다. 기존 scripts/backup_db.sh · restore_rehearsal.sh · snapshot_data.sh 를
고정 argv 로 재사용한다. 상태는 home/operations/jobs/*.json 에 남겨 API 재시작·세대 교체를 견딘다
(DB 스키마를 늘리지 않는다).
"""
