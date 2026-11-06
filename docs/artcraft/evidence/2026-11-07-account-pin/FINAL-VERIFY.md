# ArtCraft account pin — 최종 독립 검증

- 기준 코드: `0a15cecfc7a543bb402fe24208761c70aa90d037`
- 기준 문서 tip: `cf79010b41c5fea6662d5991c2d388f112cfbdb7`
- 검증 worktree: `back-artcraft-pin-final-verify-20261107`, `codex/back-artcraft-pin-final-verify-20261107`
- 판단: **독립 수용 보류**. DB 없는 경계와 정적 검사에서는 교정을 확인했지만, 완료 조건에 명시된 격리 PostgreSQL upgrade/기존 데이터 확인을 이 Codex command sandbox에서 실행할 수 없었다. 다른 engine/host로 우회하지 않았다.
- 운영 계정·설정·DB·provider·설치 변경, push, 배포는 하지 않았다.

## Run 신원 / 실행 범위

`read_session(session_id=1bb10ee5-236f-4d94-bfa8-ae4eb0bced3c, last=1)`의 head Run은 `5455d7e7-7050-4be2-9e3e-377a2b388df4`였다. 같은 UUID를 GET해 ai2 프로필을 확인한 후에만 검사를 진행했다.

```text
$ /Users/winterholic/bin/ttk get /api/runs/5455d7e7-7050-4be2-9e3e-377a2b388df4
profile=회사 코덱스 ai2 (ai2@hyperez.io)
engine=codex, model=gpt-6-luna, effort=high
```

`read_session`의 routing은 `strict_engine=true`, `task_type=verification`, `pool=personal`이었다. provider는 호출하지 않았다.

## SHA diff와 기존 발견 보존

- aab 기준 독립 검증은 별도 worktree/commit `ffdc5304b26b62f83d35d6d53de58fb25e6b9082`의 [INDEPENDENT-VERIFY.md](/Users/winterholic/development/projects/winterholic-ttakkari/back-artcraft-pin-verify-20261107/docs/artcraft/evidence/2026-11-07-account-pin/INDEPENDENT-VERIFY.md)에 보존돼 있다. 그 기록의 초기 결론은 pool 불일치로 work 계정이 막힌다는 것이었다. 이번 최종 검토는 해당 파일을 덮거나 이전 SHA의 증거를 새 SHA 증거처럼 재사용하지 않았다.
- 실제 `aab83929..0a15cecf` diff는 `resolve_task_account`의 personal pool 강제 조건 제거, manager에서 personal scope는 전체 계정 허용/ work scope는 work 그룹 유지, schema contract 0077 및 0076 online 출발점 반영, 경계 테스트 추가다.
- `0a15cecf..cf79010b` diff는 `RESULT.md`에 순차 재실행 결과 2줄을 보탠 문서-only 변경이다. 최종 tip에 새 제품 코드 변경은 없다.

```text
$ git diff --stat aab83929fc24504a6a5f11d41a011354a01e9d5a..0a15cecf
6 files changed, 122 insertions(+), 11 deletions(-)
$ git diff --stat 0a15cecfc7a543bb402fe24208761c70aa90d037..cf79010b41c5fea6662d5991c2d388f112cfbdb7
1 file changed, 2 insertions(+)
```

## DB 없는 API / manager 경계

가짜 `AsyncSession`과 fake profile 후보를 주입해 최종 SHA의 실제 resolver와 `_eligible_profiles`를 호출했다. 이 검사는 함수 경계만 실행하며 DB 통합, provider 실행 또는 전체 restart/resume loop를 대신하지 않는다.

```text
$ PYTHONPATH=src ../back/.venv/bin/python - <<'PY'
create work/enabled pin accepted id=02881d41-52f5-4826-a847-bd7c3dfc71bc
manager personal task selected only pin=['02881d41-52f5-4826-a847-bd7c3dfc71bc']
manager work task with personal account free=[] blocked=[]
create disabled pin status=503 code=account_unavailable
manager disabled pin free=[] blocked=[]
create unready pin status=503 code=account_unavailable
manager unready pin free=[] blocked=[]
create limited pin status=503 code=account_unavailable
manager limited pin free=[] blocked=['02881d41-52f5-4826-a847-bd7c3dfc71bc']
manager deleted pin free=[] blocked=[]
PY
```

## 공용 manager 도달 범위와 남은 실행 경계

코드 대조 결과 `_eligible_profiles`의 `sess.account_id is not None` 분기는 ArtCraft 전용 gate가 아니다. 해당 필드를 가진 모든 `ChatSession`이 이 공용 분기를 탄다. pin이 없는 기존 세션은 이 분기를 지나 기존 Qwen/worker/default availability 경로로 간다. `resume_queued()`는 queued Run의 session UUID를 모아 `manager.dispatch(sid)`로 다시 보내며, dispatch는 저장된 `ChatSession`을 불러온다. 이는 저장된 account_id를 재사용하는 코드 경로 근거지만, 이번 실행에서 DB 재개 loop 자체를 시험한 것은 아니다.

```text
$ rg -n "def _eligible_profiles|if sess.account_id is not None|if sess.engine == \"qwen-local\"|if task:|if pinned:|return await profiles_mod.availability|async def resume_queued|await manager.dispatch\(sid\)" src/ttakkari/runs/manager.py
2395:async def resume_queued() -> int:
2408:        await manager.dispatch(sid)
2479:async def _eligible_profiles(db: AsyncSession, sess: ChatSession, run: Run, is_meeting: bool):
2485:    if sess.account_id is not None:
2494:    if sess.engine == "qwen-local":
2513:    if task:
2515:    if pinned:
2518:    return await profiles_mod.availability(...)
```

공용 경로이므로 범위는 ArtCraft session만으로 닫히지 않는다. 이번 verifier는 다른 제품이 account_id를 저장하는지/실제 manager routing이 바뀌지 않는지에 대한 제품별 회귀를 실행하지 않았다. 별도 제품 회귀는 미검증이다. authentication/re-auth failure도 실제 profile/readiness 경로로 시뮬레이션하지 않았다. fake harness는 availability가 계정을 반환하지 않는 unready case만 호출했다. restart/resume의 실제 DB 재디스패치와 미지정 account_id 세션의 실제 선택 순서도 DB 차단으로 실행하지 않았고, 위 내용은 코드 정적 대조로 한정한다.

```text
$ sed -n '2479,2522p' src/ttakkari/runs/manager.py
if sess.account_id is not None: ... return only profile.id == sess.account_id
if sess.engine == "qwen-local": ...
if task: ...
if pinned: ...
return await profiles_mod.availability(...)
```

이 범위 역시 independent release acceptance 전에 DB 검사 허용 시 보완할 수 있지만, 이번 보고에서 실제 재개 성공/실패나 타 제품 무영향을 주장하지 않는다.

## Follow-up: 현재 Run 주입 DB에서의 독립 검증

Date: 2026-11-07 03:25 KST. 대상 제품 코드는 여전히 immutable `0a15cecfc7a543bb402fe24208761c70aa90d037`, 문서 tip `cf79010b41c5fea6662d5991c2d388f112cfbdb7`다. 이 Run은 문서 후속 `b15428b6` 위에서 검증 전용 테스트를 추가했으며 제품 `src/`는 수정하지 않았다.

### Run / DB ownership gate

`read_session(session_id=1bb10ee5-236f-4d94-bfa8-ae4eb0bced3c)`의 head는 `b875a47a-5987-483d-abea-3c3224200595`였고 routing은 `strict_engine=true`, `task_type=verification`, `pool=personal`이다. `/Users/winterholic/bin/ttk get /api/runs/b875a47a-5987-483d-abea-3c3224200595` profile은 `회사 코덱스 ai2 (ai2@hyperez.io)`, engine `codex`, model `gpt-6-luna`, effort `high`다. `TTAKKARI_TEST_DB`는 이미 주입돼 있었다. URL 문자열이나 password는 출력하지 않았다.

주입 URL의 비민감 식별자는 loopback `127.0.0.1:55672`, DB user `ttk_test`, database `ttakkari_test`였다. 이 port에 대응하는 canonical `/tmp` marker의 `run_id`는 현재 head Run과 같았다. 이어서 실제 연결의 identity를 주입 URL과 비교했다.

```text
$ ../back/.venv/bin/python - <<'PY'  # URL parsed in memory; marker matched by port; URL/password omitted
preflight=PASS host=127.0.0.1 port=55672 user=ttk_test database=ttakkari_test
owner_run_id=b875a47a-5987-483d-abea-3c3224200595 folder=ttakkari-run-test-99596-9273d4d9 owner_pid=99596
PY
$ ../back/.venv/bin/python - <<'PY'  # SELECT current_database(), current_user, inet_server_addr(), inet_server_port()
identity=PASS current_database=ttakkari_test current_user=ttk_test host=127.0.0.1/32 port=55672
PY
```

이 Run은 새 `initdb`나 `pg_sandbox.start_cluster()`를 실행하지 않았다. pytest conftest의 `TTAKKARI_TEST_DB` 명시 경로가 기존 주입 port로 `localhost` asyncpg 연결을 patch했다. conftest는 pytest 프로세스 안에서 `TTAKKARI_TEST_RUNNER=1`을 지정하므로 engine child가 자체 Run DB를 새로 만들지 않는 계약이다. migration fixture는 이 같은 cluster 안에서만 전용 migration database를 만들고 정리한다.

### Sequential test evidence

공유 fixture 검사는 병렬 실행하지 않았다. 먼저 사용자 지정 계정 집중 subset을 돌렸다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_artcraft_tasks.py -q -k 'account_id_is_persisted or work_account_can_be_pinned or personal_account_cannot_be_pinned or requested_account_must_be_enabled or deleted_pinned_account or limited_pinned_account or auth_failed_pinned_account' -p no:cacheprovider
7 passed, 32 deselected in 1.78s
```

그 subset은 work/enabled 계정의 개인 ArtCraft 허용과 역방향 work-scope 차단, 후속 pin 변경 거부, disabled/unready/quota/auth-failed 요청의 fail-closed, 삭제 계정의 peer 대체 차단을 가짜 engine/provider 경로로 확인한다. 이 수치는 이전 `RESULT.md` owner report와 구분되는 현재 Run의 독립 결과다.

추가 verifier-only `tests/test_pin_resume_verifier.py`는 API가 만든 ArtCraft session/Run을 queued 상태로 보존하고, 첫 AsyncSession 종료 뒤 새 AsyncSession에서 `sessions.account_id`와 `Run.usage.artcraft.account_id`를 다시 읽는다. 이후 pin 계정을 disabled 또는 deleted로 만든 뒤 `resume_queued()`를 실제 실행했다. 두 경로 모두 Run은 fail-closed로 끝났고, peer/emergency fallback 이벤트와 fake engine 호출은 없었다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_pin_resume_verifier.py -q -p no:cacheprovider
2 passed in 0.96s
```

초기 verifier 작성 중 fixture 미첨부로 setup 오류 2건, 이후 test fixture 인자 미첨부로 workspace 경계 실패 2건이 있었다. 이는 테스트 본문/제품 코드 결함이 아니었다. verifier 파일에 격리 ArtCraft workspace fixture를 연결한 뒤 위 최종 실행이 통과했다.

기존 주입 cluster의 `migration_database` fixture로 0076부터 기존 session row를 넣고 0077 upgrade, nullable UUID, index, FK 부재 및 단일 head를 순차 확인했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_migration_single_head.py -p no:cacheprovider
3 passed in 2.05s
```

Generation 계약 세 검사도 같은 주입 cluster에서 별도 순차 실행했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_generation_deployment.py::test_application_generation_contract_matches_its_migration_head tests/test_generation_deployment.py::test_qwen_usage_and_resource_history_migrations_are_allowed_online tests/test_collection_combined_migration_contract.py::test_c1_and_c2_form_a_serial_migration_path_and_generation_contract -p no:cacheprovider
3 passed in 0.65s
```

### Updated disposition

확인된 최종 pin 경계, persisted UUID reload, resume 후 disabled/deleted 차단, fake auth failure, 실제 0076→0077 기존 row 보존, nullable UUID/index/FK 부재 및 generation 계약을 합쳐 **최종 pin을 이 검증 범위에서 수용한다**. 이는 production deployment 승인이나 다른 제품 전용 전체 회귀 통과를 뜻하지 않는다. manager 분기는 `account_id`가 있는 모든 `ChatSession`에 공통 도달하므로 타 제품 제품별 경계는 별도 미검증으로 남긴다. 기존 Run DB 결과는 이전 `RESULT.md`의 당시 DB 소유 Run·실행 수치이며 본 Run의 독립 실행 결과로 합산하지 않는다.

원래 다른 Run에서 관측된 `initdb` `shmget(...): Operation not permitted` 실패는 해당 명령 환경의 역사적 증거로 보존한다. 현재 Run은 기존 owner-verified 주입 DB 계약으로 검증했으며 그 실패 명령을 재실행하거나 우회하지 않았다.

UUID는 임시 생성값이다. Limited manager 후보에는 지정 UUID만 있었다. account가 없어지거나 꺼진 이후 peer 후보로 바뀌지 않았다. 생성 뒤 다른 UUID를 받지 않는 API 요청 계약도 별도 fake DB harness로 확인했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/python - <<'PY'  # TaskIn/RoundIn + add_round guard
TaskIn/RoundIn UUID schema accepted; followup mismatch status=409 code=account_pin_immutable
PY
```

기존 최종 `RESULT.md`의 구현자 실행 기록은 다음 결과를 보고한다. 이는 소스 owner의 기록으로 보존하며 이번 Run의 독립 재실행 결과로 계상하지 않는다.

```text
artcraft_tasks.py focused account pin cases: 8 passed, 31 deselected
migration_single_head.py: 3 passed
generation contract checks: 3 passed
```

## 0077 migration / generation 계약

정적 Alembic/모델 대조는 migration head, contract revision, online 출발점, nullable UUID와 FK 부재를 확인했다. 실제 upgrade에서 기존 session row 보존, DB index 생성은 확인하지 못했다.

```text
$ ../back/.venv/bin/alembic -c alembic.ini heads
0077_artcraft_account_pin (head)

$ PYTHONPATH=src ../back/.venv/bin/python - <<'PY'  # metadata + contract check
alembic_heads=['0077_artcraft_account_pin']
revision=0077_artcraft_account_pin down_revision=0076_kb_v3_collection_merge
contract_schema_revision=0077_artcraft_account_pin admits_0076=True
session.account_id_nullable=True python_type=UUID fks=[]
engine_account_fk_present=False
PY
```

`RESULT.md`는 migration test가 0076에서 기존 session을 넣고 0077 upgrade 후 ID/title/engine 보존, NULL pin, PostgreSQL UUID/nullability, index, FK 부재를 확인했다고 기록한다. 본 독립 Run에서는 동일 DB 증거를 다시 만들 수 없었다.

## 격리 PostgreSQL 차단

DB 사용 전 `tests/pg_sandbox.start_cluster()`로 실제 임시 cluster를 시작하며 subprocess 예외의 stdout/stderr를 직접 받아 원인을 확인했다. 실패 단계는 `initdb` bootstrap이다.

```text
$ PYTHONPATH=tests:src ../back/.venv/bin/python - <<'PY'  # pg_sandbox.start_cluster + captured CalledProcessError
command=/opt/homebrew/opt/postgresql@17/bin/initdb -D /tmp/ttakkari-test-20555-586992a8/data -A trust -E UTF8 --locale=en_US.UTF-8 --no-sync
EXIT 1
FATAL: could not create shared memory segment: Operation not permitted
DETAIL: Failed system call was shmget(key=449271534, size=56, 03600).
```

이 오류 뒤 `initdb`는 임시 data directory를 제거했다. cluster/DB가 준비되지 않아 pytest의 계정 통합 테스트, 0076→0077 upgrade/data 보존, generation pytest는 **미실행**이다. 권한 거부가 확인되어 다른 실행 engine·명령·경로로 이 cluster 검사를 반복하지 않는다. 이는 이번 Codex command environment의 제한이며 host 전체에 대한 결론이 아니다.

## 완료한 검증과 판정

```text
$ PYTHONPATH=src ../back/.venv/bin/ruff check src/ttakkari/artcraft_tasks.py src/ttakkari/runs/manager.py tests/test_artcraft_tasks.py tests/test_migration_single_head.py tests/test_collection_combined_migration_contract.py alembic/versions/0077_artcraft_account_pin.py
All checks passed!
```

- 확인됨: work/enabled 계정의 개인 ArtCraft resolver 허용, 동일 UUID만 manager 후보로 선택, 반대 방향 work 세션에서 personal 계정 차단, disabled/unready/quota/deleted 고정의 후보 대체 방지, follow-up 변경 거부, generation 계약과 단일 head의 정적 일치.
- 기록 근거는 있음: 구현자 `RESULT.md`가 serial DB/manager integration 테스트를 보고한다. 이번 독립 검증에서는 이를 재실행하지 않았다.
- 미검증: 실제 0076→0077 격리 upgrade와 기존 row/index 확인, API→manager fake-engine 통합 및 재시작/재개 loop의 실행. 이 조건들이 완료 기준에 있으므로 **최종 pin은 릴리스 수용하지 않는다**.
- 이전 aab 결함은 최종 diff에서 해당 조건 제거로 교정됐음을 확인했지만, 독립 release acceptance는 migration DB 검증을 실행할 수 있을 때까지 보류한다.

## 재현용 Python 명령

### Manager / resolver 경계 harness

```sh
PYTHONPATH=src ../back/.venv/bin/python - <<'PY'
import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ttakkari.artcraft_tasks import resolve_task_account
from ttakkari.models import EngineAccount
from ttakkari.runs import profiles
from ttakkari.runs.manager import _eligible_profiles

work_id, personal_id, peer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
work_profile = profiles.Profile(work_id, "ai2", "codex", pool="work")
personal_profile = profiles.Profile(personal_id, "personal", "codex", pool="personal")
peer_profile = profiles.Profile(peer_id, "peer", "codex", pool="personal")
run = SimpleNamespace(usage={}, started_at=None, model=None, origin="user")

async def main():
    work_account = EngineAccount(id=work_id, name="ai2", engine="codex", pool="work", enabled=True, work_enabled=True)
    db = SimpleNamespace(get=AsyncMock(return_value=work_account))
    with patch.object(profiles, "availability", AsyncMock(return_value=([work_profile, peer_profile], []))):
        selected = await resolve_task_account(db, work_id)
        assert selected.id == work_id
    print(f"create work/enabled pin accepted id={work_id}")

    personal_sess = SimpleNamespace(engine="codex", kind="chat", strict_engine=False, pool="personal",
                                    account_id=work_id, worker_task_type=None, model=None)
    with patch.object(profiles, "availability", AsyncMock(return_value=([work_profile, peer_profile], []))):
        free, blocked = await _eligible_profiles(db, personal_sess, run, False)
        assert [p.id for p in free] == [work_id] and not blocked
    print(f"manager personal task selected only pin={[str(p.id) for p in free]}")

    work_sess = SimpleNamespace(engine="codex", kind="chat", strict_engine=False, pool="work",
                                account_id=personal_id, worker_task_type=None, model=None)
    db.get.return_value = EngineAccount(id=personal_id, name="personal", engine="codex", pool="personal", enabled=True)
    free, blocked = await _eligible_profiles(db, work_sess, run, False)
    assert free == blocked == []
    print(f"manager work task with personal account free={free} blocked={blocked}")

    db.get.return_value = EngineAccount(id=work_id, name="ai2", engine="codex", pool="work", enabled=False, work_enabled=True)
    with patch.object(profiles, "availability", AsyncMock(return_value=([work_profile], []))):
        try:
            await resolve_task_account(db, work_id)
        except Exception as exc:
            assert exc.status == 503 and exc.code == "account_unavailable"
            print(f"create disabled pin status={exc.status} code={exc.code}")
        else:
            raise AssertionError("disabled account unexpectedly accepted")
    free, blocked = await _eligible_profiles(db, personal_sess, run, False)
    assert free == blocked == []
    print(f"manager disabled pin free={free} blocked={blocked}")

    db.get.return_value = EngineAccount(id=work_id, name="ai2", engine="codex", pool="work", enabled=True, work_enabled=True)
    with patch.object(profiles, "availability", AsyncMock(return_value=([], []))):
        try:
            await resolve_task_account(db, work_id)
        except Exception as exc:
            assert exc.status == 503 and exc.code == "account_unavailable"
            print(f"create unready pin status={exc.status} code={exc.code}")
        else:
            raise AssertionError("unready account unexpectedly accepted")
        free, blocked = await _eligible_profiles(db, personal_sess, run, False)
        assert free == blocked == []
    print(f"manager unready pin free={free} blocked={blocked}")

    until = time.time() + 120
    with patch.object(profiles, "availability", AsyncMock(return_value=([], [(work_profile, until)]))):
        try:
            await resolve_task_account(db, work_id)
        except Exception as exc:
            assert exc.status == 503 and exc.code == "account_unavailable"
            print(f"create limited pin status={exc.status} code={exc.code}")
        else:
            raise AssertionError("limited account unexpectedly accepted")
        free, blocked = await _eligible_profiles(db, personal_sess, run, False)
        assert free == [] and [p.id for p, _ in blocked] == [work_id]
    print(f"manager limited pin free={free} blocked={[str(p.id) for p, _ in blocked]}")

    db.get.return_value = None
    free, blocked = await _eligible_profiles(db, personal_sess, run, False)
    assert free == blocked == []
    print(f"manager deleted pin free={free} blocked={blocked}")

asyncio.run(main())
PY
```

### Contract / schema introspection

```sh
PYTHONPATH=src ../back/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
from ttakkari.models import ChatSession, EngineAccount
source = Path.cwd()
config = Config(str(source / "alembic.ini"))
config.set_main_option("script_location", str(source / "alembic"))
directory = ScriptDirectory.from_config(config)
heads = directory.get_heads()
revision = directory.get_revision("0077_artcraft_account_pin")
contract = json.loads((source / "deploy/generation-contract.json").read_text())
column = ChatSession.__table__.c.account_id
print(f"alembic_heads={heads}")
print(f"revision={revision.revision} down_revision={revision.down_revision}")
print(f"contract_schema_revision={contract['schema_revision']} admits_0076={'0076_kb_v3_collection_merge' in contract['online_migration_from']}")
print(f"session.account_id_nullable={column.nullable} python_type={column.type.python_type.__name__} fks={list(column.foreign_keys)}")
print(f"engine_account_fk_present={any(fk.column.table.name == EngineAccount.__tablename__ for fk in column.foreign_keys)}")
PY
```
