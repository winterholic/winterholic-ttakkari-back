# ADR 0027. 외부 전사 연동 제거와 기본 로컬 런타임

- 상태: 구현·로컬 검증 완료. 운영 적용은 기획 세션의 배포 요청 후 별도 확인한다.
- ADR 0010·0021·0025·0026의 외부 연동/선택·기본 provider·테이블 정책을 대체한다. 원문 ADR은 남긴다.
- 사용자 요청: 클로바노트를 완전히 제거하되 가져온 회의록·전사와 Chrome 프로필은 보존한다.

## 실행 경계

브라우저 어댑터·동기화/가져오기/업로드 큐·API·MCP·로그인/노트 탭·관련 설정·Playwright 의존성을 제거한다.
Playwright는 제거한 브라우저 어댑터 외 런타임 사용처가 없었다. 검색 E5·일반 회의 분석·PMS·벡터 경로는 유지한다.
등록 provider는 local_whisper 하나이며, 과거 provider 문자열과 provider_job_id는 회의 기록의 출처로 유지한다.
진행 중이던 폐기 provider의 전사는 기존 reconcile 규칙으로 실패 처리되어 로컬 재전사할 수 있다. 성공한 회의록은 그대로 읽고 분석한다.

## 데이터 보존·마이그레이션 0026

정본은 meetings/meeting_items/meeting_proposals/meeting_chunks 및 원음 Artifact다.
구 두 테이블에서 meetings로만 외래키가 있으며 meetings에서는 외래키로 구 테이블을 참조하지 않는다.
provider_job_id는 외래키가 아닌 출처 문자열이다. 따라서 구 테이블 제거가 회의록을 cascade 삭제하지 않는다.

구 clovanote_notes/jobs의 **모든 행**을 retired_integration_records(source,id,payload,archived_at)에 to_jsonb 원본으로 보관한 뒤 두 활성 테이블을 삭제한다.
보관본은 외부 연결용 ORM·작업 큐·API에서 사용하지 않으며 원문/구간/화자/메타·작업 이력을 잃지 않기 위한 비활성 기록이다.
연결된 회의의 transcript만 비어 있고 노트에 전사가 있는 경우 최신 노트의 전사를 채운다. 기존 교정 원문·요약·항목·분석·벡터는 덮어쓰지 않는다.
아직 회의록으로 가져오지 않은 노트도 보관본에 남으며 새 회의록으로 자동 생성하지 않는다.

downgrade는 두 구 테이블·인덱스를 복원하고 typed jsonb_populate_record로 보관된 모든 값을 돌려준다.
그 사이 삭제된 회의를 가리키면 meeting_id만 null로 복원해 원본 노트·작업을 보존한다. 구제한 회의 전사는 downgrade에서도 지우지 않는다.
기존 스키마→새 스키마→복원, 교정 원문/전체 원시 행 일치, 비어 있는 전사 구제, 삭제된 회의 참조 복원은 독립 테스트 DB에서 검증한다.
운영 DB에는 이번 작업에서 마이그레이션을 실행하지 않는다.

## 환경파일 없이 로컬 STT 준비

기본 루트: ~/development/projects/winterholic-ttakkari/back/.stt-runtime/.
Python은 venv/bin/python, 모델은 faster-whisper-large-v3 디렉터리다. 런타임·모델은 Git에서 제외한다.
명시한 local_stt_python/model_path는 우선한다. 없으면 기본값을 사용한다. home/배포 체크아웃 경로 변경으로 런타임이 사라지지 않도록 지속 경로를 선택했다.
공개 Systran 모델 revision edaa852ec7e145841d8ffdb056a99866b5f0a478을 준비했다. 비밀값·인증 토큰은 필요하지 않다.
자식 환경 PYTHONPATH는 배포본 src로 고정해 선택 런타임에 오래된 앱 코드가 섞이지 않게 한다.
설정 extra=ignore는 유지하여 삭제한 외부 연동 및 meeting_default_provider 키가 남아도 시작이 실패하지 않도록 한다.

Whisper/E5 요청별 프로세스·빈 큐 이벤트 대기 정책은 유지한다. 프로필 ~/.ttakkari/clovanote-chrome은 삭제하거나 내용을 읽지 않는다.
상세 측정·검증·운영 스모크는 [제거 결과](../stt/remove-clovanote-2026-10-15.md)에 둔다.
