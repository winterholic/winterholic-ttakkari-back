# 2026-10-15 · 공개 조회 공유·내보내기 공통 계약 설계

이 기록은 설계 세션 시점의 상태를 보존한다. 이후 formatter·owner export·public 생성 download를 구현한 현재 상태는 [후속 작업기록](2026-10-15-public-share-exports.md)에서 확인한다.

- 범위는 설계만. engine=codex/model=gpt-6.1-sol/effort=high 유지, 프론트 읽기만, 일반 구현/디자인/모델 실행/자동 위임 없음.
- recall의 개인 앱 유지·선택 자원만 익명 조회/다운로드 결정과 최근 STT·UI API·재전사 기록을 확인했다. 현재 인증→회의 읽기 모델→PMS/자료실 정책→기존 파일 링크·브라우저 CSV/xlsx 순으로 조사했다. 원본 복구 결과함 문서는 읽기만 했다.
- 기준 backend4a07739cc6bdba75ef8d4b2877c91ea3fabf9f84. 타 세션의 공통 엔진/모델/API 미커밋 변경을 보존하고 stash/reset/commit/push하지 않는다.
- 결정: 별도 share capability, 승인한 명시적 공개 DTO snapshot, 매 요청 회수/만료/원본·선택 binding/scope 확인, 같은 투영만으로 생성 다운로드. 기존 /api/dl 또는 share_file을 공개 공유 기능으로 재활용하지 않는다.
- 정식 위치는 ADR0030과 docs/sharing/public-share-export-contract.md. owner 발급/재게시/회수·공개 GET/HEAD/download·오류·필드 허용목록·토큰/로그/캐시·정규화 전사/분석·형식별 조건·회귀 반례를 기록했다. 별도 handoff 문서는 만들지 않았다.
- 회의록 첫 수직 기능 목표는 TXT/MD/JSON/CSV다. SRT/VTT는 시각·cue 검증, DOCX/PDF는 생성 라이브러리·한글 폰트 등 추가 검증 후 활성화한다. PMS/WBS/선택 Artifact의 유한 확장 지점만 기록하고 맥락 도서관 전체 검색은 공개하지 않는다.
- 공식 근거는 URI fragment/RFC3986, HTTP cache/RFC9111, W3C Referrer Policy/WebVTT, OWASP CSV Injection, python-docx/ReportLab 문서만 확인했다. Notta 참고 이미지 web fetch는 cache miss여서 화면 내용은 확인하지 않았고 UI 설계로 확장하지 않았다.
- 배포 상태는 별도: 조사 시 운영 d61e3a0·PID61369. 이전 STT 운영 데이터 복구와 이번 미구현 공유 계약은 다른 상태다. 이 작업에서 공개 링크 발급·운영 DB 변경·실제 사용자 자료 외부 전송·모델 실행·배포/재시작을 하지 않는다.
- 검증은 문서의 상대 링크·JSON 예시·정적 계약 경계·변경 범위 점검으로 수행한다. 공유 구현이 없으므로 pytest/실제 공개 보안 동작은 미실행이며 이전671개 테스트 결과를 이 기능의 근거로 사용하지 않는다.

- 정적 검증 실행: `python3 /private/tmp/check-ttakkari-share-design.py` → 설계/연결 문서4개·상대 링크34개·JSON 예시13개 파싱, 공개 예시 allowlist/필수 경계 PASS, NUL0. `git diff --check -- <본 작업 문서>` 오류 없음. 실제 구현 테스트가 통과했다는 주장은 하지 않는다.
- 산출물 복사 제약: 사용자 지정 outbox fbbf7fc8-fff1-4bf1-9c64-7421d8bd5e39로 두 문서 복사를 시도했으나 OS PermissionError로 거절됐다. 현재 실행 쓰기 허용 outbox는 e9621e23-5b07-4155-a610-920745fc5bbf이며 사용자는 fbb만 지정했으므로 다른 결과함에는 쓰지 않았다. 정식 저장소 문서를 결과로 제공한다.
