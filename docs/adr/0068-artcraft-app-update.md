# ADR 0068: ArtCraft 공식 릴리스 앱별 안전 업데이트 및 복구

## 상태
채택 (2026-11-06)

## 배경
ADR 0035(ArtCraft 관제)에서는 자동 업데이터 부재 및 dist/ 교체 시 설치 상태 파손 위험을 이유로 설치·업데이트를 지원하지 않고 최신 릴리스 비교와 STATUS.md §6의 수동 절차 안내만 제공했다.
그러나 사용자가 폰/웹 관제 인터페이스에서 앱별로 안전하게 최신 공식 릴리스를 확인·검증·적용·복구(롤백)할 수 있는 백엔드 인터페이스를 명시적으로 요청·승인하였다.
현재 Mac Studio에는 13개 ArtCraft 앱이 설치되어 있고 다른 제작/스모크 작업이 운영 중이므로, 실행 중인 제작 작업과 충돌하지 않으면서도 임의 조작에 의한 위험을 완전히 방어하는 엄격한 안전 통제가 필요하다.

## 결정

1. **공식 Storytold 자산 한정 및 입력 통제**:
   - 앱 식별자는 `registry.json`의 13개 등록 앱 allowlist(`_APP_NAME`)만 허용한다.
   - 버전 태그는 `^v?\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$` 정규식으로 엄격 검증하며, 임의 URL/셸 명령 입력 경로는 일절 제공하지 않는다.
   - 다운로드는 오직 GitHub 공식 API/공식 릴리스(`storytold/<app>`)의 macOS Universal CLI zip 자산과 `SHA256SUMS.txt`만 수신한다.

2. **무결성 및 아카이브 보안 검증**:
   - **SHA256 필수 대조**: 다운로드한 zip 파일의 해시를 `SHA256SUMS.txt`의 공식 서명 해시와 대조하여 불일치 시 즉시 거절(`checksum_mismatch`)하고 임시 파일을 삭제한다.
   - **Zip Traversal 및 심링크 탈출 방어**: 압축 해제 시 절대 경로(`member.filename.startswith('/')`), 상위 디렉터리 탈출(`..`, `commonpath` 불일치), 대상 디렉터리 외부를 가리키는 내부 심볼릭 링크를 사전 차단(`insecure_archive`)한다.

3. **Busy Guard (실행 충돌 방어)**:
   - 해당 앱의 GUI가 켜져 있거나(`running` 또는 `listening`), 관련 Job(스모크, GUI 조작, 타 업데이트)이 활성 상태일 때는 409 Conflict(`app_running` / `busy`)로 작업을 거절한다.
   - 전체 GUI 스모크(`smoke full`)가 진행 중일 때도 모든 앱의 업데이트/롤백을 차단한다.

4. **원자적 스왑 및 백업 보존**:
   - 새 버전 압축 해제는 독립된 staging 디렉터리(`.staging-<job_id>`)에서 완료한 후, 실행 권한(`0o755`)을 부여하고 정규 경로(`dist/cli/<app>/...`)로 이동한다.
   - 직전 설치된 바이너리 경로와 버전 정보를 `.backup/backup_meta.json`에 영구 보존한다.
   - `bin/<app>-cli` 심링크는 임시 심링크 생성 후 `os.replace`로 원자적으로 교체한다.

5. **적용 후 사후 검증 및 자동 롤백 (Auto-Rollback)**:
   - 심링크 교체 직후 `--version`을 서브프로세스로 실행하여 종료 코드 0 및 정상 출력을 확인한다.
   - 사후 검증 실패 시 사용자 개입 없이 즉시 이전 백업 바이너리로 심링크를 원자적 복구하고, 500 `verification_failed` 오류 및 Job 실패를 기록한다.

6. **수동 롤백 (Manual Rollback)**:
   - `POST /api/artcraft/apps/{app}/update/rollback` 엔드포인트를 제공하여, 사용자가 원할 때 언제든 보존된 이전 버전으로 안전하게 복귀할 수 있다 (`confirm=true` 필수).

7. **상태 영속화 및 재시작 거짓 성공 방지**:
   - 업데이트 이력 및 상태는 `<data_dir>/artcraft/state.json`의 `updates` 객체에 원자적 파일 락(`update_state`)으로 기록한다.
   - 서버 재시작 시점에 `running`/`queued` 상태로 남아있던 작업은 `reconcile_stale_updates()`를 통해 `interrupted`로 정정하여 거짓 성공(false positive)을 원천 차단한다.

## 결과
- 기존 관제 API(`GET /api/artcraft/apps`, `GET /api/artcraft/jobs/{job_id}`)의 응답 스키마와 클라이언트 호환성을 100% 유지하면서, 앱별 업데이트 미리보기/적용/롤백/상태 API를 제공한다.
- 운영 중인 다른 프로세스나 제작 작업과의 충돌 없이 격리된 단위 테스트 및 로컬 fixture 검증으로 신뢰성을 보장한다.
