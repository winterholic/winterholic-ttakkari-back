"""Creative Studio AI director + independent semantic reviewer evaluation harness (isolated DB and plan store).

Phases (each is a separate process so a human or a worker can run between them):
  prepare            start an isolated PostgreSQL cluster, queue one storyboard Run per scenario, write prompts
  ingest SCEN FILE   store a real model answer as the Run result, read it back, accept it into a plan
  edit SCEN          user-edit one scene, reload the plan from disk
  review MP4 SHA     run the claude-CLI semantic reviewer through production_review.run_review
  stop               stop the cluster

Nothing here touches the production database, plan store or backend. The model answers are produced
outside this script (a codex worker Run for generation); this script never fabricates one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("CS_EVAL_ROOT", "/tmp/cs-eval-20261121"))
PG_BIN = Path("/opt/homebrew/opt/postgresql@17/bin")
PORT = 54339
DB_URL = f"postgresql+asyncpg://eval@127.0.0.1:{PORT}/cs_eval"


def configure_env() -> None:
    for d in ("home", "allowed/default-root"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "TTAKKARI_NO_ENV_FILE": "1", "TTAKKARI_ENV": "dev", "TTAKKARI_DATABASE_URL": DB_URL,
        "TTAKKARI_HOME": str(ROOT / "home"),
        "TTAKKARI_ALLOWED_ROOTS": json.dumps([str(ROOT / "allowed")]),
        "TTAKKARI_DEFAULT_WORKSPACE_ROOT": str(ROOT / "allowed/default-root"),
        "TTAKKARI_DEFAULT_WRITABLE_ROOTS": json.dumps([str(ROOT / "allowed/default-docs")]),
        "TTAKKARI_JWT_SECRET": "eval-only-secret-eval-only-secret-0123456789",
    })


def cluster(action: str) -> None:
    os.environ["LC_ALL"] = "C"  # postgres refuses to start multithreaded without a valid locale
    data = ROOT / "pg"
    if action == "start":
        if not (data / "PG_VERSION").exists():
            subprocess.run([PG_BIN / "initdb", "-D", data, "-A", "trust", "-U", "eval", "--locale=C", "-E", "UTF8"], check=True, capture_output=True)
        if subprocess.run([PG_BIN / "pg_ctl", "-D", data, "status"], capture_output=True).returncode == 0:
            return
        subprocess.run([PG_BIN / "pg_ctl", "-D", data, "-o", f"-p {PORT} -k {ROOT} -c listen_addresses=127.0.0.1",
                        "-l", ROOT / "pg.log", "-w", "start"], check=True, capture_output=True)
        subprocess.run([PG_BIN / "createdb", "-h", "127.0.0.1", "-p", str(PORT), "-U", "eval", "cs_eval"], check=False, capture_output=True)
    else:
        subprocess.run([PG_BIN / "pg_ctl", "-D", data, "-m", "fast", "stop"], check=False)


SCENARIOS = {
    "A": {
        "brief": "일본 애니메이션 스타일의 SF 공포 액션 단편을 약 2분 길이로 만든다. 우주 정거장에서 정체불명의 생명체를 피해 탈출하는 이야기이며 네온 조명과 빠른 컷을 쓴다.",
        "sources": [
            {"id": "anime-grammar", "locator": "user-provided:anime-direction-notes",
             "text": "일본 TV 애니메이션은 셀 셰이딩과 굵은 윤곽선을 쓰고, 액션 장면에서는 임팩트 프레임과 빠른 컷으로 속도감을 만든다. 공포는 느린 빌드업 뒤에 짧은 폭발적 컷을 두어 긴장을 푼다."},
            {"id": "space-station", "locator": "user-provided:setting-notes",
             "text": "배경은 궤도 정거장이다. 좁은 복도와 비상등, 네온 표지판이 주된 조명이며 우주 공간은 창밖으로만 보인다."},
        ],
        "structured": {"selections": [
            {"id": "C01.animation", "role": "primary", "strength": "required"},
            {"id": "C02.space_sf", "strength": "preferred"},
            {"id": "C03.japanese_anime", "strength": "preferred"}],
            "duration": {"mode": "target", "seconds": 120}, "aspect": "16:9"},
        "confirm": ["tone.horror", "tone.action"],
    },
    "B": {
        "brief": "Kafka의 Producer, Broker, Consumer 흐름을 16초 안에 설명하는 기술 설명 영상을 만든다.",
        "sources": [
            {"id": "kafka-intro", "locator": "https://kafka.apache.org/43/getting-started/introduction/",
             "text": "Producer는 이벤트를 Kafka에 발행하는 클라이언트 애플리케이션이다. Broker는 이벤트를 토픽의 파티션 로그에 순서대로 저장한다. Consumer는 토픽을 구독해 저장된 이벤트를 읽는다. 오프셋은 파티션 안에서 레코드의 위치를 나타낸다."},
        ],
        "structured": {"selections": [{"id": "C01.tech_explainer", "role": "primary", "strength": "required"}],
                       "duration": {"mode": "strict", "seconds": 16}, "aspect": "16:9"},
        "confirm": [],
    },
}


def build_request(key: str):
    from ttakkari.creative.v2.intent_resolve import ResolveRequest, resolve_intent
    from ttakkari.creative.v2.storyboard import StoryboardRequest

    s = SCENARIOS[key]
    resolved = resolve_intent(ResolveRequest(
        brief=s["brief"], structured=s["structured"], confirm=s["confirm"]))
    return StoryboardRequest.model_validate({
        "brief": s["brief"], "sources": s["sources"], "engine": "codex",
        "intent_v2": resolved.intent.model_dump(mode="json"),
    }), resolved


async def create_tables() -> None:
    from ttakkari.db import init_engine
    from ttakkari.models import Base

    async with init_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def prepare(attempt: int) -> dict:
    from unittest.mock import AsyncMock

    from ttakkari.api import sessions
    from ttakkari.creative.v2 import storyboard_runs
    from ttakkari.db import sessionmaker

    await create_tables()
    # The Run manager would try to launch an account-routed CLI; generation is done by an external worker Run.
    sessions.manager.dispatch = AsyncMock()
    state_path = ROOT / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    for key in SCENARIOS:
        request, resolved = build_request(key)
        async with sessionmaker()() as db:
            started = await storyboard_runs.start(db, request, request_key=f"eval-{key}-{attempt}")
        prompt_path = ROOT / f"prompt-{key}-{attempt}.txt"
        async with sessionmaker()() as db:
            from ttakkari.models import Run
            run = await db.get(Run, uuid.UUID(started["run_id"]))
            prompt_path.write_text(run.prompt)
        state.setdefault(key, {})[str(attempt)] = {
            "run_id": started["run_id"], "engine": started["engine"], "model": started["model"],
            "prompt": str(prompt_path), "ready_for_plan": resolved.ready_for_plan,
        }
        print(key, json.dumps(state[key][str(attempt)], ensure_ascii=False))
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state


async def ingest(key: str, attempt: int, answer_file: Path, executor: dict) -> None:
    from ttakkari.creative.v2 import storyboard_runs
    from ttakkari.creative.v2.plans import default_plan_store
    from ttakkari.db import sessionmaker
    from ttakkari.models import Run, RunStatus

    state = json.loads((ROOT / "state.json").read_text())
    run_id = uuid.UUID(state[key][str(attempt)]["run_id"])
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        run.status = RunStatus.succeeded
        run.result_text = answer_file.read_text()
        run.usage = {**(run.usage or {}), "executor": executor}
        await db.commit()
    async with sessionmaker()() as db:
        read = await storyboard_runs.read(db, run_id)
    print("phase:", read["phase"], read.get("error"))
    if read["phase"] != "completed_needs_review":
        return
    async with sessionmaker()() as db:
        plan = await storyboard_runs.accept(
            db, default_plan_store(), run_id, reviewed_output_sha256=read["output_sha256"])
    state[key][str(attempt)].update(plan_id=plan["id"], revision=plan["revision"], output_sha256=read["output_sha256"])
    (ROOT / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print("plan:", plan["id"], "revision", plan["revision"])


def digest(plan: dict) -> dict:
    d = plan["director"]
    return {
        "plan_id": plan["id"], "revision": plan["revision"], "aspect": plan["aspect"],
        "director": {k: d[k] for k in ("source", "model", "run_id", "output_sha256", "reviewed")},
        "intent_v2_conditions": [(c["id"], c["origin"]) for c in plan["intent_v2"]["conditions"]],
        "sequences": [(q["sequence_id"], q["title"], q["scene_ids"]) for q in d["sequences"]],
        "scenes": [{
            "scene_id": s["scene_id"], "title": s["title"], "seconds": s["seconds"], "strategy": s["strategy"],
            "preset": s.get("preset"),
            "alternatives": [(a["strategy"], a.get("reason", "")[:60]) for a in s["strategy_rationale"]["alternatives"]],
            "shots": [{
                "shot_id": sh["shot_id"], "layers": sh["layers"], "required_capabilities": sh["required_capabilities"],
                "output_contract": sh.get("output_contract"),
                "quality_criteria": [(c["id"], c["hard"]) for c in sh.get("quality_criteria", [])],
                "depends_on": sh.get("depends_on"), "retry_policy": sh.get("retry_policy"),
            } for sh in s["shots"]],
        } for s in plan["scenes"]],
    }


def load_plan(plan_id: str) -> dict:
    from ttakkari.creative.v2.plans import default_plan_store
    return default_plan_store().get(plan_id)  # PlanStore reads plan.json from disk each call


def cmd_edit(key: str, attempt: int) -> None:
    from ttakkari.creative.v2.duration import SceneEdit
    from ttakkari.creative.v2.plans import PlanStore, default_plan_store

    entry = json.loads((ROOT / "state.json").read_text())[key][str(attempt)]
    before = load_plan(entry["plan_id"])
    target = before["scenes"][1] if len(before["scenes"]) > 1 else before["scenes"][0]
    edits = [SceneEdit(id=s["scene_id"], title=s["title"], narration=s.get("narration", ""), seconds=s["seconds"])
             if s["scene_id"] != target["scene_id"] else
             SceneEdit(id=s["scene_id"], title=s["title"] + " (사용자 수정)",
                       narration="사용자가 직접 고친 해설: " + (s.get("narration") or s["title"]), seconds=s["seconds"])
             for s in before["scenes"]]
    default_plan_store().edit(entry["plan_id"], before["revision"], scenes=edits)
    fresh = PlanStore(default_plan_store().root, registry=default_plan_store().registry)  # new instance, disk only
    after = fresh.get(entry["plan_id"])
    changed = [s["scene_id"] for b, s in zip(before["scenes"], after["scenes"], strict=True)
               if (b["title"], b.get("narration")) != (s["title"], s.get("narration"))]
    print(json.dumps({
        "edited_scene": target["scene_id"], "revision_before": before["revision"], "revision_after": after["revision"],
        "changed_scene_ids": changed,
        "title_after": next(s["title"] for s in after["scenes"] if s["scene_id"] == target["scene_id"]),
        "narration_after": next(s["narration"] for s in after["scenes"] if s["scene_id"] == target["scene_id"]),
        "director_scenes_unchanged": [x["scene_id"] for x in after["director"]["scenes"]]
        == [x["scene_id"] for x in before["director"]["scenes"]],
    }, ensure_ascii=False, indent=2))


def cmd_show(key: str, attempt: int) -> None:
    entry = json.loads((ROOT / "state.json").read_text())[key][str(attempt)]
    print(json.dumps(digest(load_plan(entry["plan_id"])), ensure_ascii=False, indent=2))


def review_plan(media: Path):
    """A plan that honestly describes the pre-existing 2D walk-cycle MP4 (8.02 s) so its scenes can be reviewed."""
    from ttakkari.creative.v2.catalog_data import ENTRIES
    from ttakkari.creative.v2.duration import DurationRequest
    from ttakkari.creative.v2.intent import Extraction, InfoItem, IntentSpec, Selection
    from ttakkari.creative.v2.plans import build_plan, default_plan_store
    from ttakkari.creative.v2.planner import ProductionDirection

    assert "C08.three_act" in ENTRIES  # the structure entry the core reuse path can approve
    items = [InfoItem(id="walk", title="캐릭터 걷기", narration="2D 캐릭터가 화면을 가로질러 걷는다", essential=False)]
    intent = IntentSpec(
        selections=[Selection(entry_id="C08.three_act")], info_items=items,
        duration=DurationRequest(mode="preferred", seconds=8), aspect="16:9",
        extraction=Extraction(method="user_structured"),
    )
    direction = ProductionDirection.model_validate({
        "source": "user_structured", "reviewed": True,
        "intent": "고정 2D 캐릭터가 걷는 워크 사이클 8초",
        "scenes": [{
            "scene_id": "walk", "strategy": "frame_animation_2d", "preset": "walk_cycle",
            "rationale": "고정 walk cycle 표면만 지원하는 2D 프레임 애니메이션",
            "shots": [{"shot_id": "walk-1", "action": "2D 캐릭터가 화면 중앙을 가로질러 걷는다", "layers": ["visual", "audio"]}],
        }],
    })
    plans = default_plan_store()
    plan = build_plan(items[0].narration, intent, registry=plans.registry, director=direction)
    return plans, plans.create(plan)


def cmd_review(media: Path, expect_sha: str, model: str) -> None:
    from ttakkari.creative.v2 import dispatch, production_review, quality
    from ttakkari.creative.v2.review_cli_evaluator import claude_cli_binding

    sha = quality.file_hash(media)
    assert sha == expect_sha, f"media sha {sha} != {expect_sha}"
    plans, plan = review_plan(media)
    plan = plans.approve(plan["id"], plan["revision"])
    store = dispatch.ProductionStore(plans)
    folder = store.folder(plan["id"])
    final = folder / f"final-{sha[:16]}.mp4"
    shutil.copyfile(media, final)  # pre-existing artifact bound as the revision's final; not re-rendered here
    scenes_seconds = [s["seconds"] for s in plan["scenes"]]
    with store.tx(plan["id"]) as data:
        data["dispatches"].append({
            "revision": plan["revision"], "rubric": quality.build_rubric(plan), "log": [], "segments": [],
            "final": {"status": "qa_passed", "id": str(uuid.UUID(sha[:32])), "file": final.name, "sha256": sha,
                      "size": final.stat().st_size, "name": "creative-2d-walk-cycle.mp4",
                      "bound_preexisting_artifact": True},
        })
    print("plan scenes seconds:", scenes_seconds, "plan:", plan["id"])
    out = production_review.run_review(plans, None, plan["id"], plan["revision"], binding=claude_cli_binding(
        model, plan_scenes=plan["scenes"]))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    data = store.read(plan["id"])
    record = dispatch._record(data, plan["revision"])
    report = record["quality_report"]
    receipt = record["quality_evidence"][0]
    print(json.dumps({
        "plan_id": plan["id"], "revision": plan["revision"], "output_sha256": report["output_sha256"],
        "machine_qa": report["machine_qa"]["verdict"],
        "semantic_qa": {"verdict": report["semantic_qa"]["verdict"], "evaluators": report["semantic_qa"]["evaluators"],
                        "checks": [{k: c[k] for k in ("criterion_id", "scene_id", "verdict", "uncertainty")}
                                   for c in report["semantic_qa"]["checks"]]},
        "viewing_qa": {"verdict": report["viewing_qa"]["verdict"], "reason": report["viewing_qa"]["reason"]},
        "overall_verdict": report["overall_verdict"], "defects": report["defects"],
        "review_execution": record["review_execution"],
        "receipt_rows": [{"criterion_id": c["criterion_id"], "scene_ids": c["scene_ids"], "verdict": c["verdict"],
                          "uncertainty": c["uncertainty"], "rationale": c["rationale"],
                          "evidence_frames": [(Path(e["path"]).name, e["start_time"]) for e in c["evidence"]]}
                         for c in receipt["criteria"]],
        "execution": receipt["execution"], "evaluator": receipt["evaluator"],
        "attestation": record["quality_attestations"][0],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("prepare"); sp.add_argument("--attempt", type=int, default=1)
    sp = sub.add_parser("ingest"); sp.add_argument("scenario"); sp.add_argument("file", type=Path)
    sp.add_argument("--attempt", type=int, default=1); sp.add_argument("--executor", default="{}")
    for name in ("edit", "show"):
        sp = sub.add_parser(name); sp.add_argument("scenario"); sp.add_argument("--attempt", type=int, default=1)
    sp = sub.add_parser("review"); sp.add_argument("media", type=Path); sp.add_argument("sha256")
    sp.add_argument("--model", default="sonnet")
    sub.add_parser("stop")
    a = p.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "stop":
        cluster("stop")
        return
    if a.cmd == "prepare":
        cluster("start")
    configure_env()
    if a.cmd == "prepare":
        asyncio.run(prepare(a.attempt))
    elif a.cmd == "ingest":
        asyncio.run(ingest(a.scenario, a.attempt, a.file, json.loads(a.executor)))
    elif a.cmd == "edit":
        cmd_edit(a.scenario, a.attempt)
    elif a.cmd == "show":
        cmd_show(a.scenario, a.attempt)
    elif a.cmd == "review":
        cmd_review(a.media, a.sha256, a.model)


if __name__ == "__main__":
    sys.exit(main())
