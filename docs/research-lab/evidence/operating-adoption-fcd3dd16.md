# 중단 복구·운영 A 실측·데이터 보존

2026-10-26 리드 Codex가 재개했다. Muse Run 13ce93b6은 `Muse MSP schema
fingerprint differs from the pinned SDK`로 실패했다. 이 오류를 Research Lab이나
호스트 전체의 실행 불가로 해석하지 않았다. 저장소에서 backend151195b9와
front0daa6c6의 clean commit/push 및 인수 evidence를 확인했다. 기존 인수를 반복하지 않았다.

## 배포와 기존 데이터

`backend_deploy_status(f8f8d559-b69b-41a1-9394-e96b1c5e019d)`는 succeeded,
activated_revision=fcd3dd161406a12e32ed9a9c9a65fb5e58c207b0였다.
`curl -fsS http://127.0.0.1:8787/api/version`도 같은 revision과 PID87194를 반환했다.
이 revision이 acca010 로그 verbosity4 교정을 포함함을 git ancestor로 확인했다.

실운영 MCP로 기존 eb41e3a7-43cd-4f2b-8c03-79380a2e1163 연구를 resume했다.
12 nodes/9 relations/0 experiments/45 events/10 evidence/truncated=false였다.
기준 600eb843의 모든 기존 행 ID와 필드 SHA를 대조했다.

```text
Python canonical JSON/SHA256 field comparison
fields_compared=575; changed_or_missing=0; added_entries=1; truncated=false
```

원본 사용자 연구 본문은 저장소에 복제하지 않았다. 추가 필드는 따로 구분한
[비교 보고서](operating-baseline-comparison-fcd3dd16.json)를 남겼다. 이 한 연구의
보존을 전체 운영 DB row 수·migration lock 시간·다른 scope 보존으로 확대하지 않는다.
PMS6.3의 미식별 legacy92b3c8e7도 검증했다고 하지 않는다.

500행 이후 탐색 커밋151195b9는 아직 이 활성 버전에 없다. 현재 source4개의
SHA와 저장된6page counts를 직접 대조했다. 이 후보의 누적 배포를
7b3bac82-680d-43ec-b7c2-37f6e6102533로 예약했다(requested/skip_tests=false).
이후 Runtime report에 testing으로 관측됐으나 추가 배포 polling은 하지 않았다.

프론트는 fresh fetch 후 main이0daa6c6의 조상이고0behind/20ahead/clean임을
확인해 일반 atomic FFpush했다. 원격 main=0daa6c6bcd83ddcd9eefc1ddc1abb6dc2158e3c4.
Vercel 상태는 첫 조회 pending이었다. 기존 fcd3 API로 원래 v2 흐름을 사용할 수 있고,
새 페이지 기능은 서버 미지원 안내를 갖추었으므로 프론트 게시를 진행했다.
페이지 탐색의 운영 사용 가능 판정은 새 backend 활성 후 별도로 한다.

## 실제 A: 운영 MCP → host GPU → 저장·채택·resume

호스트 자원 조회는 CPU10.3%, memory available40.18GB/pressure normal,
GPU0%를 반환했다. 기존 swap43.51GB와 새 실행의 swap 증가량은 구분한다.
GPU lock이 비어 있음을 확인한 뒤 4B 모델만 두 설정으로 실행했다.

- Study cb031b8c-2f55-4f7d-b771-6b116732798a
- Experiment dd87c769-d339-49f6-878e-5b86dd1ac2a6
- Control Run7de4d2d5-f06b-4ec5-8dbd-fa20379148ef
- Treatment Run0b3d1f3c-e86a-4488-8dd4-e2a3f15b4382

`manage_research experiment_validate` valid=true 후 experiment_create auto_start를
실행했다. qwen3.5-4b-q4_k_m, ctx4096, FlashAttention on, V cache f16,
threads6, n_predict64, 내부 bench2회는 고정하고 Key cache만 f16→q8_0으로 바꿨다.

| 지표 | F16 Key | Q8 Key |
|---|---:|---:|
| Decode tok/s (내부2회 median) |119.9932|90.5234|
| Prefill tok/s (내부2회 median) |418.5705|389.7600|
| 근사 Prefill ms (proxy) |86.01|92.36|
| 호스트 swap 증가 peak MB |0|0|

두 Run의 run_status=succeeded, 각 measurement5개와 raw/env가 저장됐다.
compare_series는 바뀐 설정이 cache_type_k 하나임을 보존하고 decode delta
-24.5596%를 반환했다. 구성별 persisted Run은1개이며 내부 bench2회의 중앙값이다.
신뢰구간이나 다양한 입력/품질 평가를 수행하지 않았다. VRAM·순수 TTFT·
일반적인 양자화 효과를 측정했다고 하지 않는다. **호스트 GPU 경로에서 측정했다.**

cleanup은 두 Run 모두 success=true/stopped_now=true/server_exit_code=0이었다.
후속 host_gpu_status도 stopped/running=false다. 이 결과는 시험 서버 정리와
코드 경로의 host resource lock 해제 증거이며, 별도 연구별 RuntimeLease나
장시간 owner-crash/generation-drain 인수로 확대하지 않는다.

F16 설정을 이번 입력·모델·컨텍스트의 처리량 관점으로만 BestResult
46cd85b0-760e-497d-90d5-42177253c33d에 채택했다(promote_method=false).
fresh operating MCP resume에서 completed experiment와 decision/채택 event를 복원했다.
[실제 실행·비교·채택 JSON](operating-A-fcd3dd16.json)에 원본을 보존한다.

## 가려진 레이어 배치 로그의 최소 교정

실측 raw의 device_evidence는 Metal GPU name/초기화8줄만 반환했다. 현재 parser는
로그의 처음8개 장치 관련 줄을 선택하므로 이후 offloaded/model/KV buffer 줄이
있어도 가려진다. 실제 레이어 배치를 아직 보았다고 주장하지 않는다.

합성 로그 반례로 startup12줄 뒤 offload/Metal KV buffer3줄이 빠지는 문제와
limit1에서 offloaded0/33이 가려지는 문제를 재현했다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py -q --tb=short
2 failed, 1 passed in 2.20s
$ PYTHONPATH=src .venv/bin/python -m pytest --noconftest tests/test_host_gpu_device_logging.py tests/test_host_gpu.py -k 'device_evidence or build_args or start_request or request_rejects or server_enables' -q --tb=short
9 passed, 30 deselected in 1.11s
$ .venv/bin/ruff check src/ttakkari/host_gpu.py tests/test_host_gpu_device_logging.py
All checks passed!
```

기존 장치 줄만 대상으로 literal offload/buffer 보고를 먼저 선택하고 초기화 줄로
남은8자리만 채웠다. 0-layer나 placement 없는 로그도 그대로 유지한다. 명령·
GPU 플래그·lease/stop 정책은 바꾸지 않았다. diff check exit0.

새 parser 활성 후 **같은 마지막 launch의 보존 로그**를 host_gpu_status로 읽어
배치 근거를 보완한다. request/start timestamp가 바뀌었으면 이번 Run 로그라고
인정하지 않는다. 같은 모델 벤치를 무작정 반복하거나 보호 로그 파일을 직접 읽지 않는다.
실제 배치·운영 A 화면·새 페이지 운영·나머지 R4/7/12~16과 PMS 원조건은 미완이다.
