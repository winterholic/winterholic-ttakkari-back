# 2026-10-22 E1 준비 반영: managed Comfy I2V 입력·profile 계약

요청: E1 실험 담당의 `e1-prep/e1-run-spec.json`, `contract-check.json`이 짚은 본체 공백(I2V profile·기준 이미지 입력·receipt 해시)만 연결. 상세 설계와 한계는 `docs/adr/0068-creative-engine-adapters-and-director.md` 끝의 "추가: I2V 기준 이미지 입력과 준비 profile".

- 브랜치: `feat/creative-engines-director`(기존 `b5e4020` 위에 이어서). main 병합·배포 예약 없음.
- 작업 위치: 같은 격리 worktree `/tmp/back-g34`. 공유 back 작업트리·배포 설정·lab E1 폴더·registry는 읽기만 했다.

## 먼저 확인한 진행 중 변경
- 이 worktree는 `b5e4020`에서 clean이었다(미커밋 변경 없음). 이 요청으로 이미 구현된 I2V 코드는 없었다.
- origin/main은 `39f96a9`까지 진행됐지만 `managed_comfy*.py`, `api/comfy_managed.py`, `host_resource.py`, `api/creative.py`, `creative/` 변경은 0건이라 기준 계약(`2522d78`)이 그대로다. 이 브랜치는 main을 병합하지 않았다.
- 이미 있던 것을 재사용했다: G0 제작 CAS(`read_artifact`의 sha·심볼릭 링크·이탈 거부), 공용 lease·`server.start(required_gb)` 가드, 기존 stub HTTP 프로토콜 harness(실제 Runner·registry 사용), Runner의 `kind=image` 파라미터 자동 업로드.

## 구현(코드)
| 변경 | 파일 |
|---|---|
| 이미지 검증기 `InputImage` | `src/ttakkari/managed_comfy_input.py`(신규) |
| 소유 자산 해석(id+sha256, 제작 단위) | `src/ttakkari/creative/comfy_inputs.py`(신규) |
| profile `wan22-5b-i2v-draft`, `prepare(image=)`, 비공개 staging, 업로드 단계와 실패 정리, receipt 연결 | `src/ttakkari/managed_comfy.py` |
| receipt `input_image`·`profile_verification` | `src/ttakkari/managed_comfy_receipt.py` |
| `POST /api/comfy/jobs`의 `image={production_id, asset_id, sha256}` | `src/ttakkari/api/comfy_managed.py` |
| creative 샷: Comfy I2V profile, 입력 자산 이름·manifest 소유 검사 | `creative/engines/comfy_engine.py`, `creative/pipeline.py` |

기존 계약 보존: `PROFILES`의 T2V 항목과 값(`required_gb=16`) 불변, T2V `prepare`/API 호출 형태 불변(`image` 없으면 컨트롤러·API 호출 모양이 같다), T2V receipt에 새 키 없음, G0 계약 미수정. `profiles()` 출력은 T2V 네 필드를 유지하고 `input`·`verification`·`required_gb`·`required_gb_basis`를 더했으며, 목록에 I2V 항목이 늘어 `test_comfy_managed.py`의 profile 목록 기대값 한 곳만 부분 일치 + 새 항목 확인으로 바꿨다.

## E1 입력에 대한 적용
- 기준 이미지 `keyframe_flux_cafe.png`(1344×768, sha256 `0c4b9a75…e6e919`)는 이 저장소에 두지 않았다(시험은 같은 이름의 작은 대역 PNG를 쓴다). 실행 담당이 제작에 업로드해 `asset_id=keyframe_flux_cafe.png`와 위 sha256로 지정하면 된다. 자산 이름이 `keyframe_flux_cafe.png`여야 E1 준비 graph의 LoadImage 값(`ttk/keyframe_flux_cafe.png`)과 resolved hash가 같아진다.
- **E1 준비 graph와의 일치를 CPU로 확인**: 실제 Runner·registry(`i2v-wan22-5b@6c9413e014d5`, preset `draft`)를 stub HTTP 서버에 붙여 shot1(seed 2101)·shot2(seed 2102)를 실행했고, 각각 `resolved_hash == e0dc76a6e7d5`, `1c5e79fb6475`로 E1 준비 단계 값과 일치했다(`test_i2v_over_http_uploads_the_owned_bytes_once_and_reproduces_the_prepared_e1_resolved_hashes`). 이는 graph 구성 일치의 증거일 뿐 생성이 아니다.
- `required_gb`: E1 제안 16은 채택하지 않았다. profile은 24(T2V 실측 peak footprint 근거의 보수값, `required_gb_basis`에 "I2V 미측정" 명시)이고 기존 `server.start(required_gb)` 가용 메모리 대기와 공용 lease를 그대로 쓴다. 실행 직전 `server_resources`·pressure·swap 증분 확인은 가드가 아니라 실행 담당의 절차로 남아 있다(현재 swap 높음 기록 유효).

## 검증(이 환경에서 실제 실행)
- 신규·갱신 시험: `test_comfy_i2v_input.py` 37, `test_creative_comfy_i2v.py` 7, `test_comfy_managed_protocol.py` +3(I2V 성공·E1 hash 일치, 업로드 거부, T2V 무업로드), `test_comfy_managed.py` 갱신 1.
- 회귀: `PYTHONPATH=src:unit_tests python -m pytest -q unit_tests/test_creative_*.py unit_tests/test_comfy_i2v_input.py unit_tests/test_managed_comfy_receipt.py unit_tests/test_comfy_managed*.py unit_tests/test_host_resource*.py unit_tests/test_gpu_resources.py` → `306 passed, 2 skipped`(skip 2는 기본 꺼진 Chrome 실제 렌더 시험). route/보안 게이트 인벤토리(`--noconftest`) `10 passed`. DB가 필요한 경로도 이번에는 PostgreSQL 클러스터가 떠서 최종 코드에서 `tests/test_route_inventory.py tests/test_security_gate_inventory.py tests/test_creative_api.py tests/test_host_gpu.py` → `44 passed`였다. 새 라우트는 없다(기존 `/api/comfy/jobs`만 확장).
- **간헐 실패 1건**: 위 회귀 묶음을 처음 돌렸을 때 `test_host_resource_recovery_hardening.py` 등에서 12건이 실패해 `12 failed, 294 passed`였다. 같은 묶음 재실행 `306 passed, 2 skipped`, 해당 파일 단독 3회 `88 passed`로 재현되지 않았다. 이 테스트들은 실제 프로세스 목록을 열거하므로 같은 호스트의 다른 세션 프로세스 변동에 영향을 받았을 가능성이 있다(추정, 실패 원문은 보존하지 못했다). 이 변경은 `host_resource*`를 건드리지 않았다.
- 음성 대조: 변조 해시, 빈 파일, 이미지 아님, 잘린 파일, 애니메이션 WebP, GIF, 확장자 거짓, 해상도 과대/과소, 경로형·잘못된 id, sha 불일치, 다른 제작의 자산, 없는 제작(404), CAS 심볼릭 링크 바꿔치기, CAS 파일 없음·변조, staged 사본 변조·심볼릭 링크, 요청에 path/url/그래프 필드(422), 무인증(401).
- 변이 시험: 방어를 하나씩 끈 5개(staged 해시 확인, 이미지 필수 확인, sha 일치 확인, manifest 소유 확인, 업로드 실패 후 제출 차단)가 모두 시험을 실패시켰다.
- 업로드 실패: 연결 끊김·타임아웃·잘못된 반환 이름 모두 `failed` + `input_upload.prompt_submitted=false`, `runner.run` 미도달, 소유 런타임 정지, lease 해제와 `lease_release_proof`, staged 사본 삭제를 확인했다. stub HTTP 서버 거부(500)에서도 `submit_count==0`.
- 린트: `ruff check` 변경·신규 파일 통과.

## 구현 / 준비 profile / 미검증 구분
- 구현됨(CPU·stub으로 검증): 입력 계약, profile 등록, 업로드·receipt 연결, 실패 정리, API 경계.
- 준비 profile(미검증): `wan22-5b-i2v-draft`는 `verification="prepared-unverified"`다. 기능 활성화·품질 통과·자동 선택 후보가 아니다(Comfy는 explicit-lab만).
- **실제 HTTP/WS 미검증**: 실제 ComfyUI 서버와의 `/upload/image`·`LoadImage`·`/prompt` 동작은 확인하지 못했다(stub이 프로토콜 계약만 흉내 냄). WS 관측 경로는 기존과 같이 backend 환경에 `websocket-client`가 없어 `/history` 폴링만 쓴다.
- **실제 GPU 미검증**: I2V 생성, start image VAE encode 비용, 24GB 가정의 타당성, swap 증분은 실측하지 않았다. 새 영상은 0편이다. Codex Metal/ps 거부 재시도나 운영 PID 종료, 새 모델 설치, 유료 호출은 하지 않았다.
- 배포: 하지 않았다. 배포 예약도 하지 않았다. 현재 배포 revision에 `api/comfy_managed.py`가 포함되는지는 확인하지 않았고, 이 브랜치는 별도 검수 전이다.

## 알려진 한계
- 감독 LLM 응답 스키마와 피드백 API에는 자산 지정 필드가 없다. I2V 샷은 `apply_shot_plan(assets=…)` 코드 경로로만 만든다.
- `profiles()` 응답이 T2V 항목에도 새 키를 더한다. 읽기 전용 소비자는 영향이 없지만 정확히 같은 dict를 기대하는 소비자는 갱신해야 한다.
- 이미지 업로드 이름은 자산 id 그대로(`ttk/<id>`)이고 overwrite=true다. 단일 lease가 직렬화하므로 충돌은 없지만 다른 경로가 같은 이름을 올리면 덮인다(그 경우 receipt의 sha는 요청한 값이지 Comfy 쪽 파일의 실제 바이트 검증이 아니다).
