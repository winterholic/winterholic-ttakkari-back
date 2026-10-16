# 승인 대기 회의록 다운로드 오류

사용자 IMG_3569.jpeg의 다운로드 미리보기에서 `자료가 아직 준비되지 않았어요`가 남았다. 대상 mihub 회의는 운영 DB 읽기 전용 조회에서 status=review, analyzed_at=2026-10-16T00:28:32.514064Z, 전사 있음으로 확인됐다. 기존 document builder 호출은 실제로 HTTPException 409 source_not_ready를 냈다.

원인은 build_meeting_document의 허용 상태가 transcribed/analyzed뿐이고 분석 완료 판정도 analyzed만 인정한 것이다. MeetingStatus.review는 pipeline이 분석 완료 후 승인 대기 제안이 있을 때 쓰는 정상 상태다. 허용 상태와 분석 완료 판정에 review를 추가했다. 사용자 회의 상태나 제안 승인을 변경하지 않았다.

실제 HTTP 회귀검사에서 personal/work × 붙여넣기/local STT의 review 4건이 수정 전 409로 실패했고, 미준비 4개 상태 거부 검사는 통과했다. 수정 후 회사 확인 게이트, 전체 섹션, 4가지 파일 형식, 개인정보 선택 제외와 기존 공유 회수/개인회사 이동 검사를 통과했다.

검증 명령:

`TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_review_export_20261016_0959 .venv/bin/python -m pytest tests/test_public_sharing.py tests/test_meeting_exports.py tests/test_public_share_migration.py tests/test_meeting_scope.py -q --tb=short`

결과: 77 passed in 5.84s. 변경 Python Ruff: All checks passed!, git diff --check exit 0.

운영 원본 읽기 전용 트랜잭션에서 수정 builder/formatter를 실행해 여섯 섹션 available, TXT 77471 / MD 82501 / JSON 278307 / CSV actions 1957 bytes를 확인했다. 바이트는 메모리에서만 생성했으며 파일 저장·공개 링크 게시·원본 변경은 하지 않았다. 이는 수정 코드의 실제 자료 처리 검증이며 운영 HTTP 반영 완료를 뜻하지 않는다. 프론트 변경은 없다. clean commit/push 뒤 외부 감독에 배포를 예약하며 예약 수락과 운영 반영을 구분한다.
