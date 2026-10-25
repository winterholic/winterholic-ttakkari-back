# Research Lab v2 통합 2단계 최종 인수 보고서

- 작성일: 2026-10-25
- 검증 범위: 실제 백엔드 HTTP(포트 18793) + 실제 프론트엔드 Vite(포트 5195) + 격리 임시 PostgreSQL 17(포트 58731) 환경에서 Playwright 기반 E2E 인수 시나리오 A~D 검증, UX 10문 평가, 결함 교정.
- 백엔드 브랜치: `research-v2` (`/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`)
- 프론트엔드 브랜치: `research-v2` (`/Users/winterholic/development/projects/winterholic-ttakkari/front-research-v2`)

---

## 1. 검증 환경 및 실행 명령

메인 체크아웃, 운영 launchd 서비스, 운영 DB는 전혀 건드리지 않고, 전용 임시 디렉터리(`/tmp/rv2-stage2/`)에 완전히 격리된 환경을 구축하여 검증을 진행했다.

### 1.1 환경 구성
- **격리 임시 PostgreSQL 17**: 포트 `58731`, 데이터베이스 `rv2stage2`
  - 기동: `/opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /tmp/rv2-stage2/pgdata -o "-p 58731 -k /tmp/rv2-stage2" start`
  - 마이그레이션: `alembic upgrade head` (`0072_research_lab_v2 (head)`)
- **실제 백엔드 API 서버**: 포트 `18793`
  - 기동: `uvicorn ttakkari.main:app --host 127.0.0.1 --port 18793`
- **실제 프론트엔드 웹 서버**: 포트 `5195`
  - Vite 개발 서버 프록시 설정: `/api` 경로를 `http://127.0.0.1:18793`으로 프록시
- **브라우저 자동화 (Playwright)**: `/tmp/rv2-stage2/pw/`
  - 뷰포트: Desktop (1280×900), Mobile (390×844)

---

## 2. 시나리오별 검증 결과

### 2.1 시나리오 A: LLM 추론 가속화 및 최적화 (KV 캐시 양자화 & Flash Attention)
- **판정**: **PASS**
- **Study ID**: `f5cfc6b5-ea9c-4682-bb5a-1139c97381b6`
- **Experiment ID**: `a14dd6ca-bc1a-448e-aa9a-839ee455b08d` (`KV q8_0 · Flash Attention vs f16 기준 (A/B)`)
- **실행기**: 호스트 GPU llama-server (`Metal / qwen3.5-4b-q4_k_m / llama.cpp b11655`)
- **검증 절차 및 결과**:
  1. 라이브러리에서 계층별 방법 탐색 (KV 캐시, 디코딩, GPU 커널, 메모리 관리, 실행 런타임)
  2. 플래시 어텐션, 커널 퓨전, KV 양자화 상세 정보(원리, 파라미터, 관계, 논문 근거) 확인
  3. 실험 설계: Baseline(Control A: f16 / flash_attn=false) vs Treatment(B1: q8_0, B2: flash_attn=true)
  4. 실제 호스트 GPU로 3개 런 실측 수행 (`execution_origin = local_measured`)
     - `decode_tps`: 129.6 ~ 133.4 tok/s
     - `approx_prefill_ms` (`is_proxy: true`): 58.05 ~ 58.71 ms
     - `wall_seconds`: 4.97 ~ 5.65 s
     - `swap_peak_growth_mb`: 0 MB
  5. 결과 분석 화면에서 설정별 지표 차이, 변화율, Pareto 관계 확인
  6. 최선 결과 채택(BestResult) 수행 및 연구 기록(Records) 보존 확인
- **DB 저장 검증**:
  - `research_experiments` 상태 `completed`
  - `research_runs` 3건 모두 `succeeded`, `local_measured`
  - `research_measurements` 30건 불변 레코드 적재
  - `research_best_results` 채택 레코드 1건 정상 저장
- **스크린샷**:
  - `A01-dashboard-desktop.png`, `A02-new-study-desktop.png`, `A03-study-created-desktop.png`
  - `A04-library-layer-1-KV캐시-desktop.png` ~ `A04-library-layer-5-실행런타임-desktop.png`
  - `A05-detail-flash-attn-*.png`, `A05-detail-kernel-fusion-*.png`, `A05-detail-kv-quant-*.png`
  - `A06-designer-initial-desktop.png`, `A07-designer-filled-desktop.png`
  - `A08-monitor-started-desktop.png`, `A08-monitor-finished-desktop.png`, `A08-monitor-finished-mobile.png`
  - `A09-analysis-desktop.png`, `A09-analysis-mobile.png`
  - `A10-adopted-artifact-desktop.png`
  - `A11-records-desktop.png`, `A11-records-mobile.png`

---

### 2.2 시나리오 B: TTS 음성 합성 및 전처리 정규화 (Text Normalization & RTF)
- **판정**: **PASS**
- **Study ID**: `e49380b6-1c72-449f-9730-a85c87dafac2`
- **Experiment ID**: `a8466fa2-4e66-46e0-9d45-fa53e82e7ada` (`단위 의존명사 정규화 비교 (A/B: handle_measure_units)`)
- **실행기**: 로컬 CPU Supertonic 3 ONNX 음성 합성 파이프라인
- **검증 절차 및 결과**:
  1. 라이브러리에서 TTS 정규화 및 합성 방법 탐색 및 상세 다이얼로그 확인
  2. 실험 설계: `handle_measure_units` false (Baseline A) vs true (Treatment B), 평가 코퍼스 `ko_mixed_pronunciation_v1`
  3. 실제 음성 합성 12개 청크(총 88.27초 분량) 실측 수행 (`local_measured`)
     - `rtf`: 0.1387 ~ 0.1393 (실시간 대비 약 7.2배 고속 합성)
     - `synth_seconds`: 12.25 ~ 12.29 s
     - `peak_rss_mb`: 571 MB
     - `normalized_char_delta`: 7 vs 33
     - 오디오 아티팩트 WAV 파일 정상 생성 및 브라우저 오디오 재생 UI 연결
  4. 결과 분석에서 단위 의존명사 처리 시 정확한 고유어 수사 변환 확인 및 최선 결과 채택
  5. 기록(Records) 화면에서 아티팩트 및 채택 근거 확인
- **DB 저장 검증**:
  - `research_runs` 2건 `succeeded`, `local_measured`
  - `research_artifacts` 2건 정상 연계
  - `research_best_results` 채택 레코드 1건 정상 저장
- **스크린샷**:
  - `B01-new-study-desktop.png`, `B02-study-created-desktop.png`, `B03-library-tts-desktop.png`, `B04-detail-tts-tn-desktop.png`
  - `B05-designer-filled-desktop.png`, `B06-monitor-started-desktop.png`
  - `B07-monitor-finished-desktop.png`, `B07-monitor-finished-mobile.png`
  - `B08-analysis-desktop.png`, `B08-analysis-mobile.png`
  - `B09-adopted-artifact-desktop.png`
  - `B10-records-desktop.png`, `B10-records-mobile.png`

---

### 2.3 시나리오 C: RAG 검색 정확도 최적화 & 시행착오 보존
- **판정**: **PASS**
- **Study ID**: `c4cf3f88-07e1-476f-aa62-b9d1ac81ec69`
- **Experiment 1 ID**: `44863ead-f682-42d5-8d94-fe33d229bd1f` (`하이브리드 RRF 검색 vs BM25 단독 (A/B)`)
- **Experiment 2 ID**: `aa6b0dff-c5fb-4282-a22d-b5117d281ac4` (`Dense 해시 단독 검색 vs BM25 기준 (퇴보 검증)`)
- **실행기**: 로컬 Scikit-learn CPU (BM25 + Hash Embedder), 내장 한국어 기술문서 코퍼스 (`ko_tech_docs_v1`)
- **검증 절차 및 결과**:
  1. 라이브러리에서 RAG 관련 방법 탐색 및 `hybrid_retrieval` 상세 확인
  2. 실험 1 설계 및 실측: BM25 Control(A) vs Hybrid RRF Treatment(B)
     - `recall_at_20`: Control 0.9688 → Treatment 1.0000 (+3.2% 개선)
     - `ndcg_at_20`: Control 0.9845 → Treatment 0.9878 (+0.3% 개선)
     - `query_latency_ms_mean`: Control 0.93ms → Treatment 0.73ms (-21.5% 개선)
     - `wall_seconds`: 0.016s (-20.0% 소요 시간 단축)
     - 모니터 완료 확인 및 분석 화면에서 최선 결과 채택
  3. 실험 2 설계 및 실측: Dense 해시 단독 검색 vs BM25 기준 (퇴보/시행착오 검증)
     - `recall_at_20`: Control 0.9688 유지, 변경 설정은 개선 실패
     - 시스템 자동 판정: `▼ 개선 없음·퇴보`
  4. 기록(Records) 화면 검증:
     - 성공/채택된 결과는 '잘 된 것' 섹션에 보존
     - 실험 2는 `▼ 개선 없음·퇴보` 뱃지와 함께 보존되어 시행착오가 휘발되지 않고 유지됨 확인
  5. 후속 연구(C-2) 생성 시 선행 연구 C의 채택 결과 및 실험 환경 참조 연계 확인
- **DB 저장 검증**:
  - 실험 1, 2 모두 `completed`
  - Run 6건 모두 `succeeded`, `local_measured`
  - 측정치 42건 정상 저장
  - 채택 레코드 1건 정상 저장
- **스크린샷**:
  - `C01-new-study-desktop.png`, `C02-study-created-desktop.png`, `C03-library-rag-desktop.png`, `C04-detail-rag-hybrid-desktop.png`
  - `C05-designer-filled-desktop.png`, `C06-monitor-started-desktop.png`
  - `C07-monitor-finished-desktop.png`, `C07-monitor-finished-mobile.png`
  - `C08-analysis-desktop.png`, `C08-analysis-mobile.png`
  - `C09-adopted-artifact-desktop.png`
  - `C10-records-desktop.png`, `C10-records-mobile.png`
  - `C11-followup-study-desktop.png`

---

### 2.4 시나리오 D: 카탈로그 미등록 아이디어 직접 등록, 외부 결과 입력 및 승격
- **판정**: **PASS**
- **Study ID**: `657488ea-f035-46c7-9623-55155c904f64`
- **Method Slug**: `flashinfer-block-scheduler-optimization`
- **External Experiment ID**: `19de12e4-5f88-4e90-afd7-a85c2c52df27` (`외부 A100 FlashInfer 블록 스케줄러 벤치마크`)
- **External Run ID**: `e2d04568-aeb8-446f-99dd-9b4bd9b835cb` (`execution_origin = external_report`)
- **검증 절차 및 결과**:
  1. 라이브러리에서 '새 방법·가설' 모달을 통해 새 아이디어 등록:
     - 이름: `FlashInfer 블록 스케줄러 최적화`
     - 요약: `KV 블록 스케줄링 순서를 GPU L2 캐시 친화적으로 재배치하여 Prefill 레이턴시를 단축하고 디코딩 처리량을 극대화한다.`
     - 대상: LLM, 계층: GPU 커널, 목표: 속도 향상
     - 등록 직후 안내 다이얼로그(`CreatedGuide`): 실행 상태 '가설 단계', 실행기 미지원에 따른 '어댑터 필요(`adapter_required`)' 안내 노출 확인
  2. 외부 결과 등록:
     - 실험 설계실에서 실행기 `external_report`("외부 결과 등록") 자동 선택
     - '실험 만들고 결과 입력하기' 생성
     - 모니터 화면의 `ExternalReportForm`에서 외부 환경(`A100 SXM4 80GB, CUDA 12.4, FlashInfer v0.2.0, 논문 표 2`) 및 `decode_tps: 185.4` 입력 후 등록
     - 모니터 실행 목록에 `외부 보고 · 외부 벤치마크`로 런 성공 등록 확인
  3. 검증 후 승격 (Promote):
     - 방법 상세에서 '검증 후 승격' 모달 진입
     - 근거 실행으로 등록된 외부 보고 런 선택
     - "외부 결과만 있으므로 규정에 따라 '후보(candidate)'까지만 승격 가능" 경고 및 안전 규칙 확인
     - 판단 요약 작성 후 '승격' 완료
     - 방법 상세에서 상태 배지가 '가설 단계' → '후보 (외부 결과만)'으로 정상 갱신 확인
  4. 카탈로그 재사용 확인:
     - 라이브러리 검색창에 `FlashInfer` 질의 시, 신규 등록된 방법이 '후보' 상태로 카탈로그 목록에 재사용 가능하게 노출됨 확인
- **DB 저장 검증**:
  - `research_methods`에 신규 행 추가 (`lifecycle_status = candidate`)
  - `research_runs`에 `execution_origin = external_report`, `status = succeeded` 저장
- **스크린샷**:
  - `D01-new-method-form-desktop.png`
  - `D02-method-created-badge-desktop.png`, `D02-method-created-badge-mobile.png`
  - `D03-external-designer-desktop.png`
  - `D04-external-monitor-desktop.png`
  - `D05-promote-modal-desktop.png`
  - `D06-promoted-status-desktop.png`, `D06-promoted-status-mobile.png`
  - `D07-catalog-reusable-desktop.png`, `D07-catalog-reusable-mobile.png`

---

## 3. UX 10문 체크리스트 평가 (§5.2)

| 번호 | 점검 항목 | 판정 | 화면 근거 및 구현 내역 |
|:---:|---|:---:|---|
| 1 | 비교하려는 설정 간의 차이가 한눈에 보이는가? | **적합** | 분석 화면 상단에 Baseline(Control A) 대비 Treatment(B)의 변경 파라미터가 명확히 비교 표로 표시되며 설정별 지표 수치와 차이가 일목요연하게 정렬됨 (`A09`, `B08`, `C08`). |
| 2 | 측정 지표의 방향성(더 좋은 쪽)이 직관적으로 이해되는가? | **적합** | 낮을수록 좋은 지표(지연 시간, 소요 시간) 감소 시 초록색 `▲ −21.5% 개선`, 높을수록 좋은 지표(Recall) 증가 시 `▲ +3.2% 개선`, 변화 없을 시 `― 0.0%`, 퇴보 시 빨간색 `▼ 개선 없음·퇴보`로 방향과 톤이 명확함 (`C08`, `C10`). |
| 3 | 통계적 유의성/반복 측정의 신뢰도가 명확히 표현되는가? | **적합** | 단일 런(n=1) 실행 시 모든 수치 옆에 `변동성 미상 (n < 2)` 경고가 노출되고, 반복 측정 시 n수, 분포, 표준편차가 표시됨 (`A09`, `C08`). |
| 4 | 실패·퇴보한 실험도 안전하게 기록되고 보존되는가? | **적합** | 시나리오 C에서 baseline 대비 성과가 없는 dense_hash 단독 실험이 삭제되지 않고 연구 기록에 `▼ 개선 없음·퇴보` 뱃지와 함께 보존됨 (`C10`). |
| 5 | 간접 지표(Proxy)와 실제 목표 지표가 구분되는가? | **적합** | approx_prefill_ms 및 STT 점수 등에 `간접 지표 (Proxy)` 배지와 경고 안내문("실제 TTFT나 청취 품질을 완전히 대신하지 않음")이 상시 노출됨 (`A09`, `B08`). |
| 6 | 재현을 위한 환경 정보가 누락 없이 기록되는가? | **적합** | 호스트 장비 사양, 엔진 버전(llama.cpp b11655, supertonic onnx, scikit-learn), 데이터셋 버전, 시드(42)가 실행 모니터와 결과 상세에 완전 기록됨 (`A08`, `B07`, `C07`). |
| 7 | 다지표 상충(Trade-off/Pareto) 관계를 파악할 수 있는가? | **적합** | 지연 시간 vs 품질 지표 간 상충 관계가 파레토 최적점 그래프 및 프론티어 후보 목록으로 시각화됨 (`A09`). |
| 8 | 연구 맥락(가설, 목적, 제약 조건)이 실험 결과와 함께 유지되는가? | **적합** | 실험마다 가설과 선택 이유가 유지되며, 최선 결과 채택 시 `목표`, `기준 지표`, `제약·조건`, `판단 근거` 4개 필수 항목을 입력해야만 채택되도록 강제되어 연구 맥락이 결합 보존됨 (`A10`, `B09`, `C09`). |
| 9 | 카탈로그에 없는 새 아이디어를 가설로 등록하고 승격할 수 있는가? | **적합** | 라이브러리에서 '새 방법·가설' 등록 모달을 통해 목표/계층/가설을 등록하고 외부 결과 런 입력 후 '후보(candidate)' 단계로 승격하는 전체 워크플로우가 완전 작동함 (`D01`~`D07`). |
| 10 | 모바일 화면에서도 핵심 정보가 누락 없이 확인 가능한가? | **적합** | 모바일 뷰포트(390×844)에서 반응형 레이아웃으로 모니터 진행률, 분석 결과, 배지, 기록 목록, 승격 상태가 온전히 표시됨 (`A08-mobile`, `B08-mobile`, `C08-mobile`, `D06-mobile` 등). |

---

## 4. 발견 결함 및 교정 커밋 목록

통합 2단계 E2E 검증 과정에서 실서비스 수준의 완성도를 확보하기 위해 다음 4건의 결함을 발견하고 TDD 방식으로 단위 테스트 추가 후 교정했다.

1. **[Backend] `planner.validate_experiment_plan`에서 어댑터 제약조건 미검증 교정**
   - **커밋**: `e289cf3` (`fix(research): validate expanded configs against adapter constraints in planner`)
   - **내용**: 실험 설계 시 필수 파라미터가 누락된 경우 생성 전 검증 단계에서 어댑터 제약조건(`validate_config`)을 호출하여 `errors`로 걸러내도록 수정.
2. **[Frontend] `llm_inference` 공통 파라미터(`model`) 스펙 누락 교정**
   - **커밋**: `164dfef` (`fix(research): add model param spec for llm_inference in ADAPTER_COMMON`)
   - **내용**: LLM 추론 어댑터 설계 폼에서 기본 모델 선택 파라미터가 전송되지 않던 문제 해결.
3. **[Frontend] `tts_synthesis` 기본 프리셋 폴백 주입 교정**
   - **커밋**: `83a78ef` (`fix(research): fallback preset_id for tts_synthesis in buildPayload`)
   - **내용**: 사용자가 평가 문장을 직접 입력하지 않고 기본 프리셋을 선택할 때 `ko_mixed_pronunciation_v1`이 페이로드에 자동 주입되도록 보완.
4. **[Frontend] `retrieval_eval` 기본 프리셋 폴백 주입 교정**
   - **커밋**: `7b1dddf` (`fix(research): fallback preset_id for retrieval_eval in buildPayload`)
   - **내용**: RAG 평가 시 기본 코퍼스 프리셋을 비워둘 경우 `ko_tech_docs_v1`이 자동 주입되도록 보완.

---

## 5. 미검증 항목 및 사유

1. **다중 노드 분산 클러스터 환경에서의 네트워크 지연 및 파이프라인 통신**
   - **사유**: `미실행: 로컬 Mac Studio M5 Max 단일 물리 호스트 환경에서 검증을 수행하였으므로, 다중 물리 노드 간 RDMA/인터커넥트 네트워크 분산 추론은 실측 환경 부재로 검증하지 않음.`
2. **수천 개 이상의 대규모 실험 누적 시 장기적인 아카이브 쿼리 지연**
   - **사유**: `미실행: 신규 격리 데이터베이스에 4개 연구 및 수십 개 런 수준의 인수에 집중하였으며, 수만 건 단위의 대규모 부하 스트레스 테스트는 별도 벤치마크 단계로 위임함.`
