"""Actions run·job·로그 조회."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from ttakkari.github import targets, views
from ttakkari.github.errors import GithubError
from ttakkari.github.logs import view_log_window
from ttakkari.github.routes_common import (
    Account,
    Gh,
    PageQ,
    PerPageQ,
    envelope,
    ident,
    page_meta,
)
from ttakkari.github.schemas import Envelope, Job, LogWindow, RunDetail, RunSummary

router = APIRouter()
RunStatus = Literal["completed", "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success",
                    "timed_out", "in_progress", "queued", "requested", "waiting", "pending"]
StatusQ = Annotated[RunStatus | None, Query()]
BranchQ = Annotated[str | None, Query(max_length=255)]
EventQ = Annotated[str | None, Query(max_length=40, pattern=r"^[a-z_]+$")]
FilterQ = Annotated[Literal["latest", "all"], Query()]


def _object(raw, key: str) -> list:
    items = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise GithubError("gh_schema_changed")
    return items


@router.get("/repos/{owner}/{repo}/actions/runs", response_model=Envelope[list[RunSummary]])
async def runs(owner: str, repo: str, svc: Gh, account: Account, status: StatusQ = None, branch: BranchQ = None,
               event: EventQ = None, page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    br = targets.ref(branch) if branch else None
    login = await ident(svc, account)
    up = await svc.client.get(login, "runs", f"/repos/{o}/{r}/actions/runs", ttl=15, params={
        "status": status, "branch": br, "event": event, "page": page, "per_page": per_page})
    raw = up.json()
    total = raw.get("total_count") if isinstance(raw, dict) and isinstance(raw.get("total_count"), int) else None
    return envelope(up, [views.view_run_summary(x) for x in _object(raw, "workflow_runs")], login,
                    page=page_meta(up, page, per_page, total=total))


@router.get("/repos/{owner}/{repo}/actions/runs/{run_id}", response_model=Envelope[RunDetail])
async def run(owner: str, repo: str, run_id: str, svc: Gh, account: Account) -> dict:
    o, r, rid = targets.owner(owner), targets.repo(repo), targets.big_id(run_id)
    login = await ident(svc, account)
    up = await svc.client.get(login, "run", f"/repos/{o}/{r}/actions/runs/{rid}", ttl=15)
    return envelope(up, views.view_run_detail(up.json()), login)


@router.get("/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", response_model=Envelope[list[Job]])
async def jobs(owner: str, repo: str, run_id: str, svc: Gh, account: Account, filter: FilterQ = "latest",
               page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    o, r, rid = targets.owner(owner), targets.repo(repo), targets.big_id(run_id)
    login = await ident(svc, account)
    up = await svc.client.get(login, "run_jobs", f"/repos/{o}/{r}/actions/runs/{rid}/jobs", ttl=15,
                              params={"filter": filter, "page": page, "per_page": per_page})
    raw = up.json()
    total = raw.get("total_count") if isinstance(raw, dict) and isinstance(raw.get("total_count"), int) else None
    return envelope(up, [views.view_job(j) for j in _object(raw, "jobs")], login,
                    page=page_meta(up, page, per_page, total=total))


@router.get("/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", response_model=Envelope[LogWindow])
async def job_logs(owner: str, repo: str, job_id: str, svc: Gh, account: Account,
                   from_line: Annotated[int, Query(ge=1, le=10_000_000)] = 1,
                   lines: Annotated[int, Query(ge=1, le=2000)] = 500) -> dict:
    o, r, jid = targets.owner(owner), targets.repo(repo), targets.big_id(job_id)
    login, all_lines = await svc.logs.lines(account, o, r, jid)
    from datetime import UTC, datetime
    return {"data": view_log_window(all_lines, jid, from_line, lines), "page": None,
            "rate": svc.client.rate_of(login), "fetched_at": datetime.now(UTC).isoformat(),
            "cached": False, "stale": False, "account": login}
