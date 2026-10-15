# 로컬 우선 회의 전사·모델 요청 수명 검증 (2026-10-15)

기본 전사를 로컬 Whisper로 전환하고 클로바노트 연결을 명시 선택으로 바꿨다. 코드 기본값은 local_whisper/로컬 켜짐/클로바 꺼짐/클로바 자동 동기화 0초다. 운영 설정은 읽거나 바꾸지 않았고 운영 재시작·배포도 하지 않았다.

## 구현 범위

- Provider 생략 시 로컬 선택, API의 default_provider/is_default 제공. 로컬 미준비는 사유를 반환하고 외부 전사로 자동 우회하지 않는다.
- 새 녹음 기본 선택·기존 녹음 로컬 버튼, 현재 Provider에 맞는 진행 표시. 기존 원음·전사·분석·PMS·벡터·클로바 가져오기는 보존한다.
- 회의 목록/로컬 추가 폼에서 클로바 상태·작업을 조회하지 않는다. 외부 엔진·탭을 명시 선택할 때만 클로바 조회를 활성화한다.
- STT 큐의 매초 빈 큐 조회를 이벤트 대기로 바꿨다. 새 녹음 자동 처리·기존 녹음 전사 모두 회의/job 연결 커밋 후 깨운다. 구간 후보와 시작 복구도 유지한다.
- E5의 부모 프로세스 내 영구 모델 캐시를 없앴다. STT와 E5가 공통 프로세스 감독기를 사용하며 성공·실패·시간 초과·취소에서 자식을 회수한다. E5도 부모 생존을 감시한다. 모델 파일 캐시는 유지하고 요청 중 다운로드·암묵 인증을 금지한다.
- E5 요청은 직렬 처리하고 기본 CPU 스레드는 2다. 모델 명칭·1024차원·query/passage 접두사·정규화를 유지한다. 파일 캐시 미준비는 명시 오류이며 기존 어휘 검색 경로와 실패 로그는 유지한다.
- 의존성 추가·스키마 변경 없음. 화자·중첩의 실제 정확도 검증은 앞선 2차 잔여 사항 그대로다.

## 실제 모델 수명 측정

별도 검증용 부모 프로세스에서 설치된 모델 캐시만 사용했다. 정답 품질·속도 개선율을 새로 비교한 실험이 아니다.

| 작업 | 입력 | 모델 로드 | 추론 | 전체 벽시계 | 자식 최대 RSS | 종료 확인 |
|---|---|---:|---:|---:|---:|---|
| E5 | 한국어 질의 1건·1024차원 | 0.507초 | 0.020초 | 0.793초 | 1.720GB | 종료·PID 소멸 |
| Whisper baseline CPU4 | 공개 FLEURS ko 12.48초 | 1.347초 | 7.174초 | 8.719초 | 3.609GB | 종료·PID 소멸 |

검증용 부모 RSS는 두 작업 전 27,967,488바이트, 모두 처리하고 2초 대기 후 28,147,712바이트였다. 부모에는 fastembed/onnxruntime/torch/faster_whisper/ctranslate2가 import되지 않았다. 이는 부모에 무거운 런타임과 가중치가 상주하지 않은 근거이며, 운영 서버의 RSS 절감량·시스템 전체 사용량·커널 파일 캐시 해제 지연까지 측정한 것은 아니다.

명령(backend 루트):

```sh
.venv/bin/python /Users/winterholic/.ttakkari/data/outbox/dd53ccfd-2108-494b-88f7-9f1a6e3341f3/stt-lifetime-smoke.py
.venv/bin/pytest -q --tb=short
.venv/bin/ruff check src/ttakkari/model_process.py src/ttakkari/context/embedder.py src/ttakkari/context/embedder_worker.py src/ttakkari/api/meetings.py src/ttakkari/config.py src/ttakkari/meetings/providers.py src/ttakkari/meetings/local_stt src/ttakkari/meetings/clova_jobs.py tests/test_embedder_lifetime.py tests/test_stt_local_first.py tests/test_meetings.py tests/test_clovanote.py
```

frontend 루트: `npm test -- --run`, `npm run lint`, `npm run build`.

최종 전체 검사: backend **602 passed, 2 xfailed (133.45초)**. frontend **357 passed, 1 skipped**. 변경 영역 Ruff 및 frontend ESLint·TypeScript/Vite 빌드 통과. 기존 예상 실패/건너뜀을 성공 건수로 합치지 않았다. Vite의 기존 큰 번들 경고는 남아 있으며 빌드 실패는 아니다. 회의록 기능 검사는 **23 passed**이고, 프로젝트 조회 훅의 순환 의존 분리는 별도 구조 변경 커밋으로 검증했다.

원시 근거는 [model-lifetime.json](evidence/local-first/model-lifetime.json)과 같은 폴더의 검사 로그에 있다. 테스트는 운영 DB와 분리된 ttakkari_test를 사용했고 외부 전사는 가짜 클라이언트로 검증했다. 새 기능의 RED 실패를 확인한 뒤 구현하고 GREEN을 확인했다. 원음·개인 전사·비밀값은 근거 파일과 커밋에 포함하지 않는다.

## 미검증과 운영 전환

브라우저 실화면 검증은 Chromium 실행 파일 부재, 설치된 Chrome launch 직후 SIGABRT/TargetClosedError로 실행하지 못했다. [실행 제한 기록](evidence/local-first/browser-attempt.txt). UI 컴포넌트 검증을 실제 브라우저 검증으로 확대하지 않는다.

운영의 명시 환경값은 새 코드 기본값보다 우선한다. 배포 담당자는 결과함의 local-stt-runtime.env.example과 모델/Python 설치 경로를 반영해야 한다. 이 파일은 비밀값 없는 예시이며 자동 적용하지 않았다. 기존 클로바 데이터를 삭제하거나 다시 전사하지 않는다.

운영 RSS·CPU 절감률과 장시간 관찰은 미측정: 이 작업은 운영을 재시작/배포하지 않았다. E5 요청별 로드로 시작 비용이 추가된다. 실제 화자/DER·중첩 품질·GPU 가속은 [2차 기록](phase2-2026-10-15.md)의 모델 준비·주석·Metal 접근 조건을 그대로 따른다. 정답 없는 새 정확도 수치는 만들지 않는다.
