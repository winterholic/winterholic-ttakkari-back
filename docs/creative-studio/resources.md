# 자원·기술 확인표 (2026-10-20)

검증 환경은 Codex 명령 샌드박스, 격리된 API/브라우저 테스트, 현재 운영 백엔드를 구분했다. 운영 `/api/version`과 `~/.ttakkari/deploy/current`는 모두 `b1a3d162d9c2749d344d6b9a37783c6f456fc02e`였다. `/api/health`는 `ok:true`. 이 리비전은 이번 스튜디오 변경을 포함하지 않는다. 계정 한도 조회는 실제 모델 응답·재개·MCP 실행의 증거가 아니다.

| 기존 자원 | 분류 | 이번 확인과 한계 |
|---|---|---|
| 프론트엔드·백엔드 | 즉시 사용 가능 | React 19/Vite 8·FastAPI·인증을 재사용한 격리 UI/API 실행. 운영은 위 SHA, 신규 기능 운영 미검증 |
| 오케스트레이터·큐 | 일부 수정 후 사용 가능 | 기존 generation maintenance/work/drain 구조에 렌더 작업 등록. 관련 회귀 실행. 다른 생성 모델을 위한 공용 GPU 예산 연계는 미구현 |
| Claude/Codex/Gemini/Muse | 일부 수정 후 사용 가능 | 배포 리비전 어댑터·모델 카탈로그와 account_usage 조회. 현재 Codex 세션과 MCP 호출은 실제 실행. 모든 공급자의 새 응답·재개·MCP를 따로 시험하지 않았으며, Muse 이미지 권한/한도는 코딩 할당량으로 추정하지 않음 |
| Muse Image·기타 이미지 도구 | 일부 수정 후 사용 가능 | 배포 Muse 어댑터는 coding model 허용 목록 경로. Muse SDK 1.4.10 이미지 입력 타입은 확인했지만 이미지 생성 API는 미검증. 별도 사용 가능한 내장 image_gen은 이미지 3장 실제 생성·참조 일관성 시험, 업로드로 사용. 앱에서 자동 호출하는 연결은 미구현 |
| Whisper large-v3 | 즉시 사용 가능 | 기존 `.stt-runtime/venv`와 3,087,284,237-byte model.bin 확인. 기존 모델 CPU int8로 2.94초 기악 음원 실제 처리 5.424초, peak RSS 2.865GB, 발화 0. 한국어 노래 인식/정렬 검증은 아님 |
| TTS·음성 처리 | 설치 또는 추가 개발 필요 | PMS 로컬 TTS 프로젝트와 ADR 0062·설계 문서는 존재, 구현 승인 대기 범위임을 확인. 이 기능을 동작한다고 보고하지 않음 |
| FFmpeg·라이브러리 | 일부 수정 후 사용 가능 → 개발 경로 사용 가능 | 초기 PATH에 ffmpeg/ffprobe 없음. imageio-ffmpeg 0.6.0 ARM64 번들 FFmpeg 7.1 추가, H.264/AAC·zoompan·overlay·fade 실제 렌더. ffprobe는 설치하지 않았고 FFmpeg로 스트림·전체 디코드 검사 |
| M5 Max·64GB | 즉시 사용 가능(호스트 자원) | account_usage의 호스트 probe는 Metal과 M5 Max 장치를 관측. Codex probe는 Metal device=nil·sysctl/ps 차단. GPU 생성 모델의 이번 실행/속도는 미검증이며 호스트 불가로 일반화하지 않음 |
| 로컬 LLM | 일부 수정 후 사용 가능 | 기존 host_gpu 상태·로컬 Qwen 하네스 확인. host_gpu 테스트 인스턴스는 stopped. 이번 작업은 새 서버를 띄우거나 기존 모델을 중지하지 않음. 기획 자동 호출/응답은 미검증 |
| 파일·기록·PMS·관제 | 즉시 사용 가능 | 실제 PMS 프로젝트/작업 생성과 조회, 기존 home/auth/download token/Range 응답 재사용. 결과물은 outbox. 자산 보존 수명이 다르므로 프로젝트 원본은 별도 immutable 저장; 보관함 연결 후속 |
| 디자인 시스템 | 즉시 사용 가능 | 기존 토큰·PageHeader·Button·에러·navigation 등록 사용, 390px/1440px 실제 브라우저. 신규 토큰 참조 누락 0 |
| 외부 API·구독 | 일부 수정 후 사용 가능 / 개별 미검증 | 기존 account_usage의 허용 모델·한도 확인, 인증 파일/키체인 비밀 미열람. 새 유료 API/구독 도입 없음. Muse Image·외부 음악/영상 API의 자동화 약관/권한/별도 비용은 미확정이라 연결하지 않음 |

현재 사용 불가능으로 확정할 수 있는 것은 **이 Codex 명령 환경의 Metal 직접 실행**이다(이미 측정된 차단을 재시도하지 않음). 미설치·미연결·미검증을 제품 자체의 불가능 판정으로 바꾸지 않았다.

## 후보 기술

아래 메모리는 공식 안내와 이번 실측을 구분한다. 동일 Mac에서 측정하지 않은 모델의 속도·품질 순위는 정하지 않았다. 라이선스는 코드와 체크포인트를 별도로 확인해야 한다.

| 후보 | 공식 근거·라이선스·한국어/Apple Silicon | 판단과 실측 |
|---|---|---|
| ACE-Step 1.5 | [공식 저장소](https://github.com/ACE-Step/ACE-Step-1.5) MIT, 다국어 가사·MPS/MLX 경로, 10~600초 안내. [GPU 안내](https://github.com/ACE-Step/ACE-Step-1.5/blob/main/docs/en/GPU_COMPATIBILITY.md)는 2B/XL 구성·오프로딩 구분 | 한국어 보컬 우선 검증 후보. 설치/가중치/한국어 샘플·이 Mac 시간/메모리 미검증. CUDA 수치를 Mac 통합 메모리 수치로 전용하지 않음 |
| MusicGen small | [공식 AudioCraft](https://github.com/facebookresearch/audiocraft): 코드 MIT, 가중치 CC-BY-NC 4.0. 프롬프트 기반 음악이며 가사 정합 보컬 보증 없음 | CPU 실샘플 성공: 2.94초 음원/총59.571초/peak3.198GB. 별도 선택 런타임에만 설치. 테스트 음원은 최종 영상에 사용하지 않음. 한국어/보컬 품질 미검증 |
| ComfyUI | [공식 저장소](https://github.com/Comfy-Org/ComfyUI): GPL-3.0, Apple Silicon 설치 안내. 워크플로 런타임이며 자체 생성 모델이 아님; 모델별 라이선스 별도 | 설치·실행·메모리·한국어 프롬프트 품질 미검증. 최초 결과물에 불필요한 서버·커스텀 노드 추가는 보류 |
| Wan2.2 | [공식 저장소](https://github.com/Wan-Video/Wan2.2): Apache-2.0 모델. 5B 예제는 CUDA 24GB, A14B 예제는80GB 안내 | Apple Silicon 실측 없음. 이 수치로 64GB Mac 가능/불가능을 단정하지 않음. 필요한 한 장면만 후보 실험 |
| LTX-Video / LTX-2 계열 | [공식 LTX-Video](https://github.com/Lightricks/LTX-Video)는 MPS 지원을 명시. 코드 Apache-2.0. 세대별 가중치 조건이 달라 LTX-2.x까지 동일 라이선스로 간주하지 않음 | 세대·체크포인트를 먼저 고정한 뒤 단일 장면 시험. 현재 실행 시간/메모리/캐릭터/한국어 프롬프트 품질 미검증 |
| FFmpeg + Pillow | 실제 FFmpeg7.1 ARM64 번들, `--enable-gpl --enable-libx264 --enable-libass`. [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) 래퍼와 바이너리 라이선스 구분. 한국어 자막은 설치 AppleSDGothicNeo로 래스터화 | 최초 편집기 선정. 추가 브라우저 렌더러 없이 30초 MP4 4.463초, child peak656.7MB. 이미지 카메라 모션/클립 재사용·자막 지원 |
| Remotion | [공식 라이선스](https://www.remotion.dev/docs/license): 무료 적용 조건·회사 과금 구분이 있는 별도 정책. React 기반 조합에 적합 | 복잡한 모션그래픽 요구가 생기면 재검토. 현재 FFmpeg가 요구를 충족해 도입하지 않음. 실행·성능 미검증 |
| Whisper / WhisperX | 기존 Whisper는 STT이며 음악 생성이 아님. [WhisperX](https://github.com/m-bain/whisperX)는 forced alignment/단어 타임스탬프, 언어별 모델 필요 | 한국어 노래 정렬/WhisperX 미검증. 현 버전은 장면별 자막 텍스트·길이를 직접 수정; 단어 정렬 자동화 없음 |
| Demucs | [공식 Demucs](https://github.com/facebookresearch/demucs) MIT, 음원 분리 | 현 STT 런타임에 demucs 미설치 확인. 보컬 분리·한국어 노래 품질·MPS 메모리/속도 미검증. 필요 구간만 별도 프로세스 검토 |
| librosa | [공식 문서](https://librosa.org/doc/latest/index.html), 현재 문서1.0.0. 신호 처리 기반 분석용 | BPM/비트·무음 분석 후속 후보. 현재 샘플의120 BPM/비트는 합성기가 제공한 정답 메타데이터이며 분석 모델의 추정 결과가 아님 |

## 남은 검증

- Phase1 전수 조사 중 외부 공급자별 실제 응답·재개·MCP·이미지 권한과 ACE-Step/영상 후보 실측은 남아 있다. 이 표는 1차 확인표이며 모든 후보 검증 완료가 아니다.
- 프로덕션 신규 API·영속 자산·큐/재배포 복구·기존 STT/LLM 동시 부하 시험 미실행. 최근 다른 리비전의 배포 실패가 있었으므로 배포 예약 수락을 성공으로 보고하지 않는다.
- 한국어 보컬/가사 정렬과 오디오 청감 평가는 미실행. 브라우저 재생은 30초까지 도달하고 media error=null이었으나 이것만으로 음악 품질을 보증하지 않는다.
