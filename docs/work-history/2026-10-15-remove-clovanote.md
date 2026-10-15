# 2026-10-15 · 클로바노트 완전 제거 작업 기록

- 사용자 결정: 외부 연동을 제거하고 가져온 회의록/전사 및 Chrome 프로필은 보존한다. 이번 backend 배포 요청은 기획 세션 담당.
- 기준: backend069c88b(운영 succeeded는 사용자 제공), frontaa69e62.
- 어댑터/큐/동기화/API/MCP/설정/Playwright·로그인/탭/가져오기/외부 실패 UI 제거. 공용 VectorLine 유지.
- migration0026: 구 노트/작업 모든 행 JSONB 보관 후 활성 테이블 제거, 빈 회의 전사만 구제. downgrade typed복원 및 삭제된 회의 참조 null로 보존. 기존 교정/본문/분석/벡터 불변.
- 기본 .stt-runtime/venv/bin/python 및 faster-whisper-large-v3 준비, 공개 model.bin SHA69f74147... 일치. 배포본 PYTHONPATH 전달. 구 설정 키 extra=ignore·현행 CPU설정 적용 검사.
- 실제 공개12.48초 baseline: load1.361s/infer7.189s/wall21.166s/RSS3.618GB, exit/PID부재/부모모델미로드 확인.
- backend전체596 passed/2xfailed(128.04s), front351 passed/1skipped·lint/build exit0. Chrome실화면 launch실패로 미검증.
- 비밀파일 직접열람/수정 없음. 초기Vite자동env로드 차단누락 확인 후 envDirfalse로 검사재실행.
- Playwright/pyee만lock제거, 기존버전동일. 프로필폴더 존재만확인·내용조회/삭제없음.
- ADR0027 및 docs/stt/remove-clovanote-2026-10-15.md에 판단·설치·배포후API스모크 정리. 커밋SHA·Vercel상태는 최종 결과함 요약에 기록한다.
- ~/work-history는 실행쓰기허용범위밖이므로 이 저장소의 작업기록과 결과함 입력용기록으로 남긴다.

- 프론트 단계적 배포 호환: 구 서버가 외부 provider를 default로 보내는 회귀 표본을 추가해 실패를 확인하고, 로컬만 선택/전송하도록 제한했다. 최신 front d51120a3c76ac58b18656e6f2ca2224c0cb6b466, 전체351/1skipped 재검사 및 lint/build 확인.
