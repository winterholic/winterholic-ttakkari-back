"""맥락 공유 시험 공용 도구. 합성 데이터와 계약을 따르는 인메모리 Gateway control 가짜."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ttakkari.context_sharing.gateway import GatewayReply
from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxSource,
    Workspace,
)
from ttakkari.orchestrator.runtime import issue_token

RAW_KEY_PREFIX = "synthetic-gateway-key-"


class FakeGateway:
    """코어 control 앱(context_gateway/apps.py create_control_app)의 라우트·검증·부작용을 메모리로 따라 한다.

    코어와 같은 점: verified_at 필수(422), 문서 sha256 검증(409), 최신 스냅샷을 폐기하면 이전 스냅샷이 최신으로 되살아남,
    키 수명은 서버가 정함(ttl_seconds 무시). 계약상 추가 요청 사항: 게시 영수증에 project_id 를 싣는다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.projects: dict[str, dict[str, Any]] = {}  # slug -> {id, latest, snapshots: [ids]}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, dict[str, Any]] = {}
        self.fail_with: Exception | None = None
        self.reply_status: int | None = None
        self.fail_paths: dict[str, int] = {}
        self.echo_wrong_hash = False
        self.omit_project_id = False  # 현재 코어 계약처럼 영수증에 project_id 가 없는 경우
        self.key_lifetime: int | None = None  # None 이면 요청 ttl 을 따른다. 코어 현재 동작은 3600 고정.

    def latest(self, slug: str) -> str | None:
        return self.projects[slug]["latest"]

    async def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> GatewayReply:
        self.calls.append((method, path, json))
        if self.fail_with is not None:
            raise self.fail_with
        for needle, status in self.fail_paths.items():
            if needle in path:
                return GatewayReply(status, {"detail": "forced"})
        if self.reply_status is not None:
            return GatewayReply(self.reply_status, {"code": "synthetic_failure", "message": "forced"})
        if (method, path) == ("POST", "/control/v1/snapshots"):
            return self._publish(json)
        parts = path.strip("/").split("/")
        if method == "POST" and parts[:3] == ["control", "v1", "snapshots"] and parts[-1] == "revoke":
            return self._revoke_snapshot(parts[3])
        if (method, path) == ("POST", "/control/v1/keys"):
            return self._issue_key(json)
        if method == "POST" and parts[:3] == ["control", "v1", "keys"] and parts[-1] == "revoke":
            key = self.keys.get(parts[3])
            if key is None:
                return GatewayReply(404, {"detail": "not found"})
            key["revoked"] = True
            return GatewayReply(200, {"revoked": True, "key_id": parts[3]})
        return GatewayReply(404, {"detail": "not found"})

    def _publish(self, body: dict[str, Any]) -> GatewayReply:
        if not body.get("verified_at") or not body.get("documents") or not body.get("source_refs"):
            return GatewayReply(422, {"detail": "invalid"})
        manifest_docs = []
        for d in body["documents"]:
            digest = hashlib.sha256(d["content"].encode()).hexdigest()
            if d.get("sha256") not in (None, digest):
                return GatewayReply(409, {"detail": "document hash mismatch"})
            manifest_docs.append({"id": d["id"], "title": d["title"], "content_type": d["content_type"],
                                  "sha256": "0" * 64 if self.echo_wrong_hash else digest})
        project = self.projects.setdefault(body["project_slug"], {"id": str(uuid.uuid4()), "latest": None, "snapshots": []})
        sid = str(uuid.uuid4())
        self.snapshots[sid] = {"slug": body["project_slug"], "revoked": False, "body": body}
        project["snapshots"].append(sid)
        project["latest"] = sid
        receipt = {} if self.omit_project_id else {"project_id": project["id"]}
        return GatewayReply(201, {**receipt, "id": sid, "source_refs": body["source_refs"],
                                  "manifest_sha256": hashlib.sha256(sid.encode()).hexdigest(), "documents": manifest_docs})

    def _revoke_snapshot(self, sid: str) -> GatewayReply:
        snap = self.snapshots.get(sid)
        if snap is None:
            return GatewayReply(404, {"detail": "not found"})
        snap["revoked"] = True
        project = self.projects[snap["slug"]]
        if project["latest"] == sid:  # 코어는 폐기한 최신 대신 이전 스냅샷을 최신으로 되살린다.
            alive = [x for x in project["snapshots"] if not self.snapshots[x]["revoked"]]
            project["latest"] = alive[-1] if alive else None
        return GatewayReply(200, {"revoked": True, "snapshot_id": sid})

    def _issue_key(self, body: dict[str, Any]) -> GatewayReply:
        (pid, grant), = body["projects"].items()
        if pid not in {p["id"] for p in self.projects.values()}:
            return GatewayReply(422, {"detail": "one or more projects do not exist"})
        key_id = str(uuid.uuid4())
        raw = RAW_KEY_PREFIX + uuid.uuid4().hex
        ttl = self.key_lifetime or body["ttl_seconds"]
        self.keys[key_id] = {"raw": raw, "project_id": pid, "revoked": False, "scopes": grant["scopes"], "body": body}
        return GatewayReply(201, {"key_id": key_id, "key": raw,
                                  "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()})


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def make_project(name: str, docs: dict[str, str], *, scope: str = "personal", root: str | None = None,
                       category: str = "projects", type_: str = "project", enabled: bool = True) -> dict[str, Any]:
    """색인된 프로젝트를 DB 에 직접 만든다. 디스크 파일은 만들지 않는다(어댑터는 DB 만 읽는다)."""
    root = root or f"/synthetic-root/{uuid.uuid4().hex[:8]}/{name}"
    async with sessionmaker()() as db:
        entity = CtxEntity(type=type_, name=name, scope=scope, summary=f"{name} 요약")
        db.add(entity)
        await db.flush()
        source = CtxSource(path=root, scope=scope, category=category, entity_id=entity.id, enabled=enabled)
        db.add(source)
        await db.flush()
        ids: dict[str, str] = {}
        for rel, body in docs.items():
            doc = CtxDocument(source_id=source.id, path=f"{root}/{rel}", title=rel.rsplit("/", 1)[-1],
                              sha256=sha(body), chunk_count=1)
            db.add(doc)
            await db.flush()
            db.add(CtxChunk(document_id=doc.id, ord=0, heading=None, content=body))
            ids[rel] = str(doc.id)
        await db.commit()
        return {"entity_id": str(entity.id), "source_id": str(source.id), "docs": ids, "root": root}


async def agent_headers() -> dict[str, str]:
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:6]}", root_path="/synthetic-root/ws")
        db.add(ws)
        await db.flush()
        session = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="agent", engine="codex", pool="personal")
        db.add(session)
        await db.commit()
        return {"Authorization": f"Bearer {issue_token(session.id)}"}
