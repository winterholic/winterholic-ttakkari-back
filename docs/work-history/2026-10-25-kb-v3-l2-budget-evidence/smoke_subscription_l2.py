"""Real subscription-CLI smoke for the L2 transport on synthetic public passages.

Run from the repo root with a throwaway environment so no operational DB/config is touched:
  TTAKKARI_NO_ENV_FILE=1 TTAKKARI_HOME=<tmp> TTAKKARI_DATABASE_URL=postgresql+asyncpg://invalid@127.0.0.1:1/none \
  PYTHONPATH=src .venv/bin/python <this file> <codex|claude>
It builds a Profile by hand (default CLI login, no DB account row), so quota cooldown persistence
fails closed (logged) instead of writing anywhere.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

from ttakkari.kb_pipeline.extract_l2 import L2_SCHEMA
from ttakkari.kb_pipeline.subscription_transport import (
    L2TransportError,
    SubscriptionCallConfig,
    execute_subscription_l2_call,
)
from ttakkari.runs.profiles import Profile

PASSAGES = [
    ("PostgreSQL pgvector",
     "pgvector is an open-source extension for PostgreSQL that stores vectors and supports "
     "exact and approximate nearest neighbor search. It supports HNSW and IVFFlat indexes."),
    ("Redis 개요",
     "Redis는 메모리 기반 키-값 저장소이며 캐시, 메시지 브로커, 세션 저장소로 쓰인다. "
     "RDB 스냅샷과 AOF 로그로 디스크 영속성을 제공한다."),
    ("Kubernetes",
     "Kubernetes schedules containers onto nodes. A Deployment manages ReplicaSets, and a "
     "ReplicaSet keeps a stable set of Pod replicas running."),
]


async def main(engine: str) -> int:
    profile = Profile(id=uuid.uuid4(), name=f"smoke-{engine}", engine=engine, config_dir=None, pool="personal")
    config = SubscriptionCallConfig(engine=engine, timeout_seconds=150.0)
    report = []
    for heading, body in PASSAGES:
        prompt = f"[제목 / 헤딩]: {heading}\n[절 경로]: 없음\n[패시지 본문]:\n{body}"
        started = time.monotonic()
        entry = {"heading": heading, "body_chars": len(body)}
        try:
            data = await execute_subscription_l2_call(profile, prompt, L2_SCHEMA, config)
        except L2TransportError as exc:
            entry |= {"ok": False, "diagnosis": exc.diagnosis, "consumed": exc.consumed,
                      "retry_after": exc.retry_after}
        else:
            quotes = [x["evidence_quote"] for x in data["assertions"] + data["relations"]]
            entry |= {
                "ok": True,
                "entities": [e["name"] for e in data["entities"]],
                "assertions": len(data["assertions"]),
                "relations": len(data["relations"]),
                "quotes_total": len(quotes),
                "quotes_exact_substring": sum(1 for q in quotes if body.find(q) >= 0),
                "sample": (data["assertions"] or data["relations"] or [None])[0],
            }
        entry["elapsed_s"] = round(time.monotonic() - started, 1)
        report.append(entry)
        if not entry["ok"] and entry["diagnosis"] == "usage_limit":
            break
    print(json.dumps({"engine": engine, "results": report}, ensure_ascii=False, indent=2))
    return 0 if all(e["ok"] for e in report) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "codex")))
