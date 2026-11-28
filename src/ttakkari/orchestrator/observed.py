"""서버가 관측한 Run 이벤트로 완료 주장과 진전을 판정한다. LLM 을 부르지 않는다.

기존 Run 이벤트(`tool.call`·`tool.result`·`run.diff`)만 읽고 새 저장소·테이블을 만들지 않는다. 관측 결과는 Run 이 끝날
때 `run.observed` 이벤트 한 건으로 남겨 다음 조회(작업자·기획 풀)가 이벤트를 다시 훑지 않게 한다.

증거 출처 구분(KINDS). 문자열 증거는 하위 호환으로 남지만 그 자체를 검증된 사실로 보지 않는다.
- self_claim: 에이전트가 쓴 주장. 서버가 대조할 관측이 없다.
- observed_tool / observed_file / observed_git / observed_test / observed_external: 서버가 본 도구 결과·파일 변경·
  Git 결과·테스트 결과·외부(배포 확인 등) 결과와 맞았다. ref 에 source_run_id, event seq, call_id, 대상, 결과, 시각이 붙는다.
- review: 코드로 못 정하는 의미 판단을 사람·기획이 정성 검토한 기록(원본 항목 대응·범위 제외 등).
- unverified: 대조할 관측이 없거나 환경상 검증할 수 없다. 거부하지 않고 미확인으로 남긴다.

운영 관측(§12)에서 셀 수 있는 이벤트:
- `completion.evidence_mismatch`(Run 이벤트, 작업자 완료 보고가 관측과 어긋남: codes, count)
- `work.evidence_rejected`(감사 로그 action, 기획 complete 체크포인트가 관측과 어긋나 거부됨)
- `work.no_progress`(Run 이벤트, 진전 없이 끝난 Run: repeated 유형별 횟수, unchanged_continuations)
- `work.continued`(기존 Run 이벤트, 자동 이어가기 1회. progressed·unchanged_continuations·auto_continuations 필드 추가)
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import ChatSession, Run, RunEvent, SessionKind, Workspace

OBS_VERSION = 1
KINDS = ("self_claim", "observed_tool", "observed_file", "observed_git", "observed_test", "observed_external",
         "review", "unverified")
SCAN_TYPES = ("tool.call", "tool.result", "run.diff")
SCAN_PAGE = 500  # 이벤트를 이 단위로 keyset 페이지 조회한다. 한 번에 메모리에 올리는 양의 상한.
# 안전 상한일 뿐이다. 예전 4000 건 상한은 긴 Run 의 후반(특히 Run 끝에 한 번 남는 run.diff)을 잘라 냈다.
MAX_EVENTS_SCANNED = 200_000
POOL_RUNS = 60  # 완료 증거를 대조할 때 보는 최근 Run 수(같은 세션·하위 작업자·같은 부모의 형제)
MAX_POOL_SCANS = 10  # 관측 캐시(run.observed)가 없는 Run 을 이벤트에서 다시 만드는 최대 수
MAX_WEAK_STREAK = 2  # 조사·새 실패 원인만으로 진전을 인정하는 연속 Run 상한
LEDGER_CAP = 300

WRITE_TOOLS = frozenset({
    "write", "edit", "multiedit", "notebookedit", "notebook_edit", "apply_patch", "str_replace_editor", "patch",
    "write_file", "edit_file", "create_file", "file_write", "write_to_file", "replace_file_content",
    "multi_replace_file_content", "file_change"})
SHELL_TOOLS = frozenset({"bash", "shell", "exec", "run_command", "command_execution"})
_META_TOOLS = frozenset({"todowrite", "todo_write", "update_plan", "exitplanmode", "manage_work"})

COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|merge|cherry-pick)\b")
_PUSH_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b")
TEST_CMD_RE = re.compile(
    r"pytest|npm (?:run )?test|swift test|cargo test|go test|vitest|jest|unittest|\bmake test|ruff check", re.IGNORECASE)
SHELL_WRITE_RE = re.compile(
    r"(?:^|[\s;&|])(?:sed -i|tee |mv |cp |rm |mkdir |touch |git add|npm install|uv add|pip install)|>>?\s*\S")
_DEPLOY_CMD_RE = re.compile(
    r"\b(?:vercel|netlify|flyctl|fly\s+deploy|wrangler|kubectl\s+(?:apply|rollout)|helm\s+(?:upgrade|install)|"
    r"docker\s+push|gcloud\s+\S+\s+deploy|firebase\s+deploy|deploy(?:\.sh)?)\b", re.IGNORECASE)
_DEPLOY_CHECK_RE = re.compile(
    r"\bvercel\s+(?:ls|list|inspect)\b|\bgh\s+api\s+\S*deployments|\bgh\s+run\s+(?:view|watch|list)\b|"
    r"\bkubectl\s+rollout\s+status\b|\bcurl\b", re.IGNORECASE)

_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")
_ERRORS_RE = re.compile(r"(\d+)\s+errors?\b")
_RUFF_FAIL_RE = re.compile(r"Found\s+\d+\s+errors?", re.IGNORECASE)
_OK_RE = re.compile(r"\bOK\b|All checks passed|tests? passed|exit code 0|Executed \d+ tests", re.IGNORECASE)
_TEST_LINE_RE = re.compile(r"(\d+)\s+passed|(\d+)\s+failed|\bOK\b|tests? passed|exit code 0|Executed \d+ tests",
                           re.IGNORECASE)
_FAILED_ID_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_COMMIT_OUT_RE = re.compile(r"\[[^\]\n]*?\s([0-9a-f]{7,40})\]")
_PUSH_OUT_RE = re.compile(r"\b([0-9a-f]{7,40})\.{2,3}([0-9a-f]{7,40})\b")
_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
# 아무것도 바꾸지 않은 커밋·푸시·병합. 성공으로 끝나도 새 성과가 아니다(명령 표현만 바꿔 되풀이해도 진전이 아니다).
_NOTHING_RE = re.compile(
    r"nothing (?:added )?to commit|no changes added|Everything up-to-date|Already up[- ]to[- ]date", re.IGNORECASE)


_NO_ID = frozenset({"", "unknown", "none", "null"})  # 엔진이 식별자를 못 줄 때 넣는 자리표시자(copilot 은 "unknown")


def _call_id(payload: dict[str, Any]) -> str | None:
    cid = payload.get("call_id")
    return None if cid is None or str(cid).strip().lower() in _NO_ID else str(cid)


def _h(text: Any, n: int = 8) -> str:
    return hashlib.sha1(str(text).encode()).hexdigest()[:n]


def _sig(text: str) -> str:
    """숫자·UUID 를 지운 문구의 지문. 같은 실패의 반복과 새 원인을 가른다."""
    norm = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", (text or "").lower())
    return _h(" ".join(re.sub(r"\d+", "#", norm).split())[:300], 10)


def _is_sha(token: str) -> bool:
    return bool(re.search(r"\d", token) and re.search(r"[a-f]", token))


_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def norm_path(path: str) -> str:
    """경로의 임시 UUID(Run 별 outbox 등)를 정규화한다."""
    return _UUID_RE.sub("<id>", str(path or "").strip())


def norm_cmd(cmd: str) -> str:
    """명령 표현을 줄인다: `cd x &&` 접두, 파이프 뒤, 리다이렉트, 플래그를 지운다. 표현만 바꾼 같은 명령을 같게 본다."""
    text = re.sub(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", cmd or "")
    text = re.split(r"\s*(?:\|\||\||&&|;)\s*", text, maxsplit=1)[0]
    text = re.sub(r"\d?>&?\d?\s*\S*", " ", text)
    toks = [t.strip("'\"") for t in text.split() if not t.startswith("-")]
    return " ".join(toks)[:200]


# 이름에 pytest 가 들어갈 뿐 테스트를 실행하지 않는 명령(검색·출력·설치·git). 종료 코드 0 만으로 통과로 읽으면
# `grep pytest x` 한 줄이 테스트 성공 증거가 된다.
_NON_EXEC_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:grep|egrep|fgrep|rg|ag|cat|bat|echo|printf|ls|find|head|tail|less|more|wc|which|whereis|type|man|"
    r"stat|file|diff|sed|awk|nl|open|code|vim|nano|git|gh|pip3?|brew|uv\s+(?:add|remove|pip|sync|lock)|"
    r"npm\s+(?:i|install|ci|uninstall|view|info|ls|list)|pnpm\s+(?:add|install|remove)|yarn\s+(?:add|install|remove))\b",
    re.IGNORECASE)


def _test_segment(cmd: str) -> re.Match[str] | None:
    """명령(`&&`·`;` 로 이은 조각 포함)에서 테스트를 실제로 실행하는 첫 조각의 TEST_CMD_RE 일치와 그 조각."""
    text = re.sub(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", cmd or "")
    for seg in re.split(r"\s*(?:&&|\|\||;|\n)\s*", text):
        if not _NON_EXEC_RE.match(seg) and (m := TEST_CMD_RE.search(seg)):
            return m
    return None


def is_test_cmd(cmd: str) -> bool:
    return _test_segment(cmd) is not None


def test_target(cmd: str) -> str:
    """테스트 명령이 겨냥한 대상. `uv run pytest tests/a.py -q` 와 `pytest tests/a.py` 는 같은 대상이다."""
    m = _test_segment(cmd)
    if not m:
        return ""
    seg = re.split(r"\s*(?:\|\||\||&&|;|>|2>&1)\s*", m.string[m.start():], maxsplit=1)[0]
    rest = seg[m.end() - m.start():].split()
    keep: list[str] = []
    skip = False
    for i, tok in enumerate(rest):
        if skip:
            skip = False
            continue
        if tok in ("-k", "-m") and i + 1 < len(rest):
            keep.append(f"{tok}={rest[i + 1]}")
            skip = True
        elif tok.startswith(("-k=", "-m=")):
            keep.append(tok)
        elif tok.startswith("-"):
            continue
        else:
            keep.append(tok.strip("'\""))
    return f"{m.group(0).lower().replace(' ', '_')}:{' '.join(sorted(set(keep)))}"


def parse_test_output(output: str, ok: bool, code: Any = None) -> dict[str, Any]:
    """테스트 출력에서 결과를 읽는다. outcome: passed | failed | unknown(요약을 못 읽음)."""
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    tail = "\n".join(lines[-15:])
    passed = [int(x) for x in _PASSED_RE.findall(tail)]
    failed = [int(x) for x in _FAILED_RE.findall(tail)]
    errors = [int(x) for x in _ERRORS_RE.findall(tail)]
    ids = sorted(set(_FAILED_ID_RE.findall(output or "")))
    if (failed and failed[-1] > 0) or (errors and errors[-1] > 0) or _RUFF_FAIL_RE.search(tail) or ids or not ok:
        outcome = "failed"
    elif passed or _OK_RE.search(tail) or code == 0:
        outcome = "passed"
    else:
        outcome = "unknown"
    line = next((ln.strip()[:160] for ln in lines[-12:] if _TEST_LINE_RE.search(ln)), "")
    fail_sig = ""
    if outcome == "failed":
        fail_sig = _h(",".join(ids), 10) if ids else _sig(" ".join(lines[-3:]))
    return {"outcome": outcome, "passed": passed[-1] if passed else None, "failed": failed[-1] if failed else None,
            "sig": fail_sig, "line": line}


def patch_signatures(patch_text: str) -> dict[str, tuple[str, str]]:
    """`git diff --unified=0` 본문에서 파일마다 (내용 지문, 역방향 지문). 줄 번호는 보지 않는다.

    역방향 지문은 이 패치를 되돌리는 패치의 지문이다. 같은 파일을 고쳤다 되돌리는 반복을 진전으로 세지 않는 데 쓴다."""
    out: dict[str, tuple[str, str]] = {}
    path: str | None = None
    added: list[str] = []
    removed: list[str] = []

    def flush() -> None:
        if path is not None:
            fwd = _h("\n".join(removed) + "\x00" + "\n".join(added), 12)
            inv = _h("\n".join(added) + "\x00" + "\n".join(removed), 12)
            out[path] = (fwd, inv)

    for line in (patch_text or "").splitlines():
        if line.startswith("diff --git "):
            flush()
            added, removed = [], []
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            path = m.group(2) if m else None
        elif line.startswith(("+++ ", "--- ", "index ", "@@")):
            continue
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    flush()
    return out


def _iso(at: Any) -> str | None:
    return at.isoformat() if isinstance(at, datetime) else (str(at) if at else None)


def _paths_of(name: str, raw: Any) -> list[str]:
    if not isinstance(raw, dict):
        return []
    found: list[str] = []
    for k in ("file_path", "path", "filePath", "FilePath", "TargetFile", "notebook_path", "notebookPath", "file"):
        if isinstance(raw.get(k), str) and raw[k]:
            found.append(raw[k])
    changes = raw.get("changes")
    if isinstance(changes, list):
        found += [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
    return found[:10]


class Observer:
    """이벤트를 순서대로 먹여 한 Run 의 관측을 만든다. 호출과 결과는 엔진이 준 call_id 로 잇는다.

    예전에는 마지막 tool.call 이 테스트 명령인지를 하나의 test_pending 으로 들고 다음 tool.result 를 거기에 붙였다.
    병렬 호출이나 사이에 낀 다른 도구 결과가 있으면 엉뚱한 결과를 테스트 결과로 읽거나 진짜 결과를 놓쳤다.
    call_id 가 없는 옛 이벤트는 미해결 호출이 정확히 하나일 때만 잇고, 둘 이상이면 연결 불가로 세지 않는다."""

    def __init__(self, git_tracked: bool = False):
        self.git_tracked = git_tracked
        self.calls: dict[str, dict[str, Any]] = {}
        self.events = 0
        self.unlinked = 0
        self.files: list[dict[str, Any]] = []
        self.new_files: list[str] = []
        self.writes: list[dict[str, Any]] = []
        self.shell_writes: list[str] = []
        self.commits: list[dict[str, Any]] = []
        self.tests: list[dict[str, Any]] = []
        self.deploys: list[dict[str, Any]] = []
        self.shas: list[str] = []
        self.inspect: list[str] = []
        self.failed: Counter[str] = Counter()
        self.ok_calls = 0
        self.failed_calls = 0
        self.meaningful_calls = 0

    # ---- 입력 ----
    def feed(self, seq: int, type_: str, payload: dict[str, Any] | None, at: Any = None) -> None:
        self.events += 1
        payload = payload or {}
        if type_ == "run.diff":
            self._diff(seq, payload)
        elif type_ == "tool.call":
            self._call(seq, payload, at)
        elif type_ == "tool.result":
            self._result(seq, payload, at)

    def _diff(self, seq: int, payload: dict[str, Any]) -> None:
        for f in payload.get("files", []) or []:
            if isinstance(f, dict) and f.get("path"):
                self.files.append({"path": str(f["path"]), "added": f.get("added"), "removed": f.get("removed"),
                                   "sig": f.get("sig"), "isig": f.get("isig"), "seq": seq})
        self.new_files += [str(p) for p in payload.get("new_files", []) or []]

    def _call(self, seq: int, payload: dict[str, Any], at: Any) -> None:
        name = str(payload.get("name") or "").lower().rsplit("__", 1)[-1]
        raw = payload.get("input")
        cmd = str((raw.get("command") or raw.get("cmd") or "") if isinstance(raw, dict) else raw or "")
        cid = _call_id(payload)
        key = cid or f"seq:{seq}"
        if name in WRITE_TOOLS:
            kind = "write"
        elif name in SHELL_TOOLS or (isinstance(raw, dict) and "command" in raw):
            kind = "shell"
        else:
            kind = "other"
        self.calls[key] = {"key": key, "id": cid, "seq": seq, "at": _iso(at), "name": name,
                           "kind": kind, "cmd": cmd, "paths": _paths_of(name, raw), "raw": raw if isinstance(raw, dict) else {},
                           "done": False}
        if name not in _META_TOOLS:
            self.meaningful_calls += 1

    def _link(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        cid = _call_id(payload)
        if cid:
            call = self.calls.get(cid)
            if call is None or call["done"]:
                return None
            return call
        pending = [c for c in self.calls.values() if not c["done"]]
        return pending[0] if len(pending) == 1 else None

    def _result(self, seq: int, payload: dict[str, Any], at: Any) -> None:
        call = self._link(payload)
        if call is None:
            self.unlinked += 1
            return
        call["done"] = True
        code = payload.get("exit_code")
        ok = not payload.get("is_error") and code in (0, None)
        output = str(payload.get("output") or "")
        call["ok"] = ok
        if ok:
            self.ok_calls += 1
        else:
            self.failed_calls += 1
        base = {"seq": call["seq"], "call_id": call["id"], "at": call["at"]}
        if call["kind"] == "write":
            if ok:
                for p in call["paths"] or [""]:
                    self.writes.append({**base, "path": p, "bytes": (call["raw"] or {}).get("bytes")})
            else:
                self.failed["write"] += 1
            return
        if call["kind"] == "shell":
            self._shell(call, base, ok, code, output)
            return
        if call["name"] in _META_TOOLS:
            return
        if ok and output.strip():
            target = next((str(call["raw"][k]) for k in ("file_path", "path", "pattern", "query", "url", "command")
                           if call["raw"].get(k)), "")
            self.inspect.append(f"i:{_h(call['name'] + '|' + target)}")

    def _shell(self, call: dict[str, Any], base: dict[str, Any], ok: bool, code: Any, output: str) -> None:
        cmd = call["cmd"]
        special = False
        if re.search(r"\bgit\b|\bgh\b", cmd):
            for tok in _HEX_RE.findall(output):
                if _is_sha(tok) and tok not in self.shas:
                    self.shas.append(tok)
        if COMMIT_RE.search(cmd):
            special = True
            kind = "push" if _PUSH_RE.search(cmd) else "commit"
            if not ok or _NOTHING_RE.search(output):
                self.failed["commit"] += 1
            else:
                sha = sha2 = None
                if kind == "push":
                    if m := _PUSH_OUT_RE.search(output):
                        sha, sha2 = m.group(1), m.group(2)
                elif m := _COMMIT_OUT_RE.search(output):
                    sha = m.group(1)
                self.commits.append({**base, "kind": kind, "sha": sha, "sha2": sha2, "cmd_key": _h(norm_cmd(cmd), 8)})
        if is_test_cmd(cmd):
            special = True
            parsed = parse_test_output(output, ok, code)
            if parsed["outcome"] == "failed":
                self.failed["test"] += 1
            self.tests.append({**base, "target": test_target(cmd), **parsed})
        check = bool(_DEPLOY_CHECK_RE.search(cmd))
        if check or _DEPLOY_CMD_RE.search(cmd):
            special = True
            if not ok:
                self.failed["deploy"] += 1
            self.deploys.append({**base, "kind": "check" if check else "deploy", "ok": ok, "cmd": norm_cmd(cmd)[:120]})
        if SHELL_WRITE_RE.search(cmd) and ok:
            special = True
            self.shell_writes.append(_h(norm_cmd(cmd), 8))
        if not special and ok and output.strip():
            self.inspect.append(f"i:sh:{_h(norm_cmd(cmd))}")

    # ---- 결과 ----
    def finish(self, run_id: Any = None) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        for f in self.files:
            ph = _h(f["path"])
            if f.get("sig"):
                k, inv = f"d:{ph}:{f['sig']}", (f"d:{ph}:{f['isig']}" if f.get("isig") else None)
            else:
                k, inv = f"d:{ph}:{f['added']}:{f['removed']}", f"d:{ph}:{f['removed']}:{f['added']}"
            facts.append({"k": k, "inv": inv, "cat": "diff"})
        for p in self.new_files:
            facts.append({"k": f"n:{_h(p)}", "inv": None, "cat": "newfile"})
        for c in self.commits:
            ident = (c["sha"] or c["sha2"] or f"x{c['cmd_key']}")[:12] if c["kind"] == "commit" else \
                f"{(c['sha'] or '')[:8]}..{(c['sha2'] or c['cmd_key'])[:8]}"
            facts.append({"k": f"{'c' if c['kind'] == 'commit' else 'p'}:{ident}", "inv": None, "cat": c["kind"]})
        if not self.git_tracked:  # git 이 보는 작업 공간은 run.diff 가 진짜 변경이다. 쓰기 호출은 되돌려도 남아 근거가 못 된다.
            for w in self.writes:
                p = norm_path(w["path"])
                is_outbox = "/outbox/" in p or p.startswith("outbox/")
                cat = "inspect" if is_outbox else "write"
                # Edit 류는 입력에 본문 크기가 없다. 경로만 키로 쓰면 같은 파일의 이후 수정이 영영 반복으로 읽히므로 Run 단위로 센다.
                facts.append({"k": f"w:{_h(p)}:{w.get('bytes') or 'r' + str(run_id or '')[:8]}", "inv": None, "cat": cat})
            for s in self.shell_writes:
                facts.append({"k": f"s:{s}", "inv": None, "cat": "write"})
        for k in self.inspect:
            facts.append({"k": k, "inv": None, "cat": "inspect"})
        seen: set[str] = set()
        uniq = []
        for f in facts:
            if f["k"] not in seen:
                seen.add(f["k"])
                uniq.append(f)
        return {
            "v": OBS_VERSION, "run_id": str(run_id) if run_id else None, "events": self.events,
            "calls": self.meaningful_calls, "ok_calls": self.ok_calls, "failed_calls": self.failed_calls,
            "unresolved_calls": sum(1 for c in self.calls.values() if not c["done"]), "unlinked_results": self.unlinked,
            "git_tracked": self.git_tracked,
            "files": self.files[:40], "file_count": len({f["path"] for f in self.files} | set(self.new_files)),
            "new_files": self.new_files[:40], "writes": self.writes[:40], "shell_writes": len(self.shell_writes),
            "commits": self.commits[-20:], "tests": self.tests[-12:], "deploys": self.deploys[-10:],
            "shas": self.shas[-60:], "failed": dict(self.failed), "facts": uniq[:120],
        }


async def _iter_events(db: AsyncSession, run_id: Any) -> AsyncIterator[Any]:
    """keyset 페이지로 이벤트를 읽는다. 앞에서 N 건만 읽고 자르지 않으므로 Run 후반의 증거도 빠지지 않는다."""
    last, total = -1, 0
    while total < MAX_EVENTS_SCANNED:
        rows = (await db.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.payload, RunEvent.created_at)
            .where(RunEvent.run_id == run_id, RunEvent.seq > last, RunEvent.type.in_(SCAN_TYPES))
            .order_by(RunEvent.seq).limit(SCAN_PAGE))).all()
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1].seq
        total += len(rows)


async def observe_run(db: AsyncSession, run: Run) -> dict[str, Any]:
    """한 Run 의 관측. 이벤트를 한 번 훑는다."""
    obs = Observer(git_tracked=bool(run.checkpoint_ref))
    async for row in _iter_events(db, run.id):
        obs.feed(row.seq, row.type, row.payload, row.created_at)
    return obs.finish(run.id)


# ---- 풀: 같은 세션·하위 작업자·형제 작업자의 최근 관측 -------------------------------------------------------
async def pool_observations(db: AsyncSession, sess: ChatSession, current: Run | None = None,
                            current_obs: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[Path]]:
    """증거 대조에 쓸 관측들(오래된 Run 부터)과 코드 저장소 후보 경로.

    이전 Run·다른 작업자의 유효한 증거는 다시 실행하지 않고 재사용한다. 종료한 Run 의 관측은 `run.observed` 이벤트를
    읽고, 없을 때만(최대 MAX_POOL_SCANS 개) 이벤트에서 다시 만든다. 지금 끝나는 Run 은 항상 새로 센다."""
    if sess.kind == SessionKind.orchestrator:
        ids = [sess.id, *await db.scalars(select(ChatSession.id).where(ChatSession.parent_id == sess.id))]
    else:
        ids = [sess.id]
        if sess.parent_id:
            ids += list(await db.scalars(select(ChatSession.id).where(
                ChatSession.parent_id == sess.parent_id, ChatSession.id != sess.id)))
    runs = list(await db.scalars(select(Run).where(Run.session_id.in_(ids))
                                 .order_by(Run.created_at.desc(), Run.id.desc()).limit(POOL_RUNS)))
    if current is not None and all(r.id != current.id for r in runs):
        runs.insert(0, current)
    cached: dict[Any, dict[str, Any]] = {}
    if runs:
        for rid, payload in (await db.execute(
                select(RunEvent.run_id, RunEvent.payload)
                .where(RunEvent.run_id.in_([r.id for r in runs]), RunEvent.type == "run.observed")
                .order_by(RunEvent.seq))).all():
            cached[rid] = payload
    out: list[dict[str, Any]] = []
    scans = 0
    partial = len(runs) >= POOL_RUNS
    for r in runs:  # 새 것부터
        if current is not None and r.id == current.id:
            obs = current_obs or await observe_run(db, r)
        elif r.id in cached and (cached[r.id] or {}).get("v") == OBS_VERSION:
            obs = cached[r.id]
        elif scans < MAX_POOL_SCANS:
            scans += 1
            obs = await observe_run(db, r)
        else:
            partial = True
            continue
        obs = {**obs, "run_id": str(r.id), "created_at": _iso(r.created_at), "session_id": str(r.session_id)}
        out.append(obs)
    out.reverse()  # 오래된 Run 부터. 테스트 "가장 최근" 판정이 이 순서를 쓴다.
    if partial:  # 60개를 넘는 오래된 Run 이나 관측 캐시 없는 Run 을 못 봤다는 표시(merge_pool 이 pool["partial"] 로 옮긴다)
        out.insert(0, {"partial": True})
    roots: list[Path] = []
    ws_ids = {r.workspace_id for r in runs if r.workspace_id}
    if sess.workspace_id:
        ws_ids.add(sess.workspace_id)
    if ws_ids:
        for p in await db.scalars(select(Workspace.root_path).where(Workspace.id.in_(ws_ids))):
            if p and Path(p) not in roots:
                roots.append(Path(p))
    return out, roots


def merge_pool(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """오래된 순 관측들을 한 묶음으로 합친다."""
    pool: dict[str, Any] = {"runs": len(observations), "calls": 0, "files": {}, "shas": set(), "commits": [], "tests": [],
                            "deploys": [], "writes": {}, "has_change": False,
                            "partial": False}
    pool["runs"] = sum(1 for o in observations if not o.get("partial"))
    for obs in observations:
        if obs.get("partial"):
            pool["partial"] = True
            continue
        rid = obs.get("run_id")
        pool["calls"] += int(obs.get("calls") or 0)
        for f in obs.get("files") or []:
            pool["files"].setdefault(f["path"], {"run_id": rid, "seq": f.get("seq")})
        for p in obs.get("new_files") or []:
            pool["files"].setdefault(p, {"run_id": rid, "seq": None})
        for w in obs.get("writes") or []:
            if w.get("path"):
                pool["writes"].setdefault(w["path"], {"run_id": rid, "seq": w.get("seq"), "call_id": w.get("call_id")})
        pool["shas"].update(s.lower() for s in obs.get("shas") or [])
        for c in obs.get("commits") or []:
            pool["commits"].append({**c, "run_id": rid})
            for s in (c.get("sha"), c.get("sha2")):
                if s:
                    pool["shas"].add(s.lower())
        pool["tests"] += [{**t, "run_id": rid} for t in obs.get("tests") or []]
        pool["deploys"] += [{**d, "run_id": rid} for d in obs.get("deploys") or []]
        if obs.get("files") or obs.get("new_files") or obs.get("writes") or obs.get("commits") or obs.get("shell_writes"):
            pool["has_change"] = True
    return pool


# ---- 주장 대조 -------------------------------------------------------------------------------------------
# 이 문장은 성공을 주장하는 것이 아니라 못 했다·모른다고 말한다. 주장으로 보지 않고 미확인으로 남긴다.
_NO_CLAIM_RE = re.compile(
    r"미실행|미수행|미확인|못\s*(?:했|함|하)|불가|하지\s*못|확인\s*필요|환경.{0,12}(?:없|부족|제한)|"
    r"\bnot (?:run|executed)\b|\bunable\b|\bcould not\b|\bcouldn't\b|\bcannot\b|\bskipped\b", re.IGNORECASE)
# "미확인 항목 없음", "환경 문제 없이" 는 못 했다는 말이 아니라 문제가 없다는 말이다. 이 표현이 문장 전체를 면제하면 안 된다.
_NEGATED_RE = re.compile(
    r"(?:미실행|미수행|미확인|불가|확인\s*필요)\s*(?:항목|사항|건|내용)?\s*[은는이가]?\s*(?:없|0\s*건|전무|아님|아니)|"
    r"환경\s*(?:에서\s*)?(?:문제|이슈|제약|제한|오류|이상)\s*[가는]?\s*없|"
    r"\b(?:no|zero|nothing)\s+(?:\w+\s+){0,2}(?:skipped|unverified|unable)\b|\bnot\s+(?:skipped|unverified)\b",
    re.IGNORECASE)
_FAIL_REPORT_RE = re.compile(
    r"(?<![\d.])[1-9]\d*\s*(?:failed|failures?|errors?)\b|(?:실패|오류|에러)\s*[1-9]\d*|[1-9]\d*\s*(?:건|개)\s*(?:실패|오류)",
    re.IGNORECASE)
_PASS_COUNT_RE = re.compile(r"(\d+)\s*(?:passed|건\s*통과|개\s*통과|통과)", re.IGNORECASE)
_TEST_FAMILIES = (("ruff_check", re.compile(r"ruff|린트|\blint\b", re.IGNORECASE)),
                  ("pytest", re.compile(r"pytest", re.IGNORECASE)),
                  ("vitest", re.compile(r"vitest", re.IGNORECASE)), ("jest", re.compile(r"\bjest\b", re.IGNORECASE)))
_TEST_CLAIM_RE = re.compile(
    r"(?P<n>\d+)\s*(?:passed|통과)|"
    r"(?:pytest|테스트|\btests?\b|ruff|vitest|jest|swift test|cargo test|go test|npm (?:run )?test|린트|\blint\b)"
    r"[^.\n]{0,40}?(?:통과|성공|passed|\bpass\b|green|\bOK\b)|"
    r"(?:통과|성공|passed|green)[^.\n]{0,12}(?:pytest|테스트)", re.IGNORECASE)
_SHA_CLAIM_RE = re.compile(
    r"(?:커밋|commit|sha|revision|리비전|HEAD|푸시|push)\w*\s*(?:sha|해시|hash|id)?\s*[:=#]?\s*`?"
    r"((?:[0-9a-f]{7,40}\b[\s,/·`]*){1,5})", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.(?:py|tsx?|jsx?|mjs|md|json|ya?ml|toml|swift|sql|sh|css|html|go|rs|java|kt|txt|ini|cfg))"
    r"(?::\d+)?(?![\w/])")
_CHANGE_VERB_RE = re.compile(r"수정|추가|작성|변경|생성|구현|만들|고쳤|고침|\b(?:created|modified|added|updated|wrote|fixed|implemented|edited)\b",
                             re.IGNORECASE)
_NOT_CHANGE_RE = re.compile(r"삭제|제거|이름\s*변경|\b(?:removed|deleted|renamed)\b|필요|해야|예정|할\s*것|\bshould\b|\btodo\b",
                            re.IGNORECASE)
_DEPLOY_CLAIM_RE = re.compile(
    r"배포[^.\n]{0,20}(?:완료|성공|됨|되었|반영|Ready)|운영\s*반영|\bdeployed\b|\bdeploy(?:ment)? (?:succeeded|complete|completed)\b",
    re.IGNORECASE)
_NOT_DEPLOYED_RE = re.compile(r"예약|예정|진행\s*중|대기|scheduled|pending|queued|in progress", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]


def split_declared(sent: str) -> tuple[str, list[str]]:
    """문장을 쉼표·세미콜론 절로 나눠 못 했다고 쓴 절을 떼어 낸다. (주장으로 남은 부분, 떼어 낸 절).

    한 문장에 `테스트는 미실행, 커밋 abc1234 푸시 완료` 처럼 섞어 쓰면 뒤 주장까지 면제되던 우회를 막는다."""
    kept: list[str] = []
    declared: list[str] = []
    for part in re.split(r"(?<=[,;，；])\s*", sent):
        (declared if _NO_CLAIM_RE.search(_NEGATED_RE.sub(" ", part)) else kept).append(part)
    return " ".join(kept), declared


def _test_family(sent: str) -> str | None:
    for prefix, rx in _TEST_FAMILIES:
        if rx.search(sent):
            return prefix
    return None


async def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, TimeoutError):
        return 128, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def _is_git(root: Path) -> bool:
    return root.is_dir() and (await _git(root, "rev-parse", "--git-dir"))[0] == 0


async def _commit_exists(root: Path, sha: str) -> bool:
    rc, out = await _git(root, "cat-file", "-t", sha)
    return rc == 0 and out == "commit"


async def _path_exists(root: Path, path: str) -> bool:
    cand = Path(path)
    if (cand if cand.is_absolute() else root / cand).exists():
        return True
    rc, out = await _git(root, "ls-files", "--", f"*{cand.name}")
    return rc == 0 and any(line == path or line.endswith("/" + path.lstrip("./")) for line in out.splitlines())


def _ref(kind: str, **kw: Any) -> dict[str, Any]:
    return {"kind": kind, **{k: v for k, v in kw.items() if v is not None}}


def claimed_shas(sentence: str) -> list[str]:
    found: list[str] = []
    for m in _SHA_CLAIM_RE.finditer(sentence):
        for tok in _HEX_RE.findall(m.group(1).lower()):
            # 커밋·SHA 낱말 바로 뒤의 토큰이라 글자만 있는 `deadbeef` 도 주장으로 본다(숫자만인 것은 날짜·건수와 겹쳐 제외).
            if re.search(r"[a-f]", tok) and tok not in found:
                found.append(tok)
    return found


def _sha_observed(sha: str, shas: set[str]) -> bool:
    return any(s.startswith(sha) or sha.startswith(s) for s in shas if min(len(s), len(sha)) >= 7)


async def check_claims(text: str, pool: dict[str, Any], roots: list[Path]) -> dict[str, Any]:
    """완료 문장의 주장을 서버 관측과 맞춘다. contradictions 만 거부 사유다. 관측이 없거나 환경상 못 맞추면 unverified.

    - 커밋 SHA: 풀의 git 출력에 있거나 저장소에 실제로 있어야 한다. 저장소가 없어 못 보면 미확인.
    - 테스트 성공: 가장 최근 관측 테스트가 통과여야 하고, 통과 개수를 주장하면 관측한 개수와 맞아야 한다.
      관측된 도구 호출이 하나도 없으면(대조할 기록 자체가 없음) 거부하지 않고 미확인으로 둔다.
    - 파일 수정: 이름을 든 파일이 관측된 변경이거나 실제로 존재해야 한다.
    - 배포 성공: 관측된 마지막 배포 명령이 실패했고 뒤에 성공한 배포·확인이 없으면 모순.
    `미실행`·`불가`처럼 못 했다고 쓴 문장은 주장이 아니다."""
    contradictions: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    have_obs = pool.get("calls", 0) > 0
    git_roots = [r for r in roots if await _is_git(r)]
    tests = pool["tests"]
    decided = [t for t in tests if t["outcome"] in ("passed", "failed")]

    partial = bool(pool.get("partial"))  # 풀이 잘려 일부 Run 을 못 봤다: 기록이 없다는 사실만으로 모순이라 할 수 없다
    for raw in sentences(text):
        sent, declared = split_declared(_URL_RE.sub(" ", raw))
        if declared and any(_TEST_CLAIM_RE.search(d) or claimed_shas(d) or _DEPLOY_CLAIM_RE.search(d) for d in declared):
            unverified.append({"code": "declared_unverified", "detail": raw[:120]})
        if not sent.strip():
            continue
        for sha in claimed_shas(sent):
            if _sha_observed(sha, pool["shas"]):
                hit = next((c for c in pool["commits"] if (c.get("sha") or "").startswith(sha) or sha.startswith(c.get("sha") or "-")), {})
                verified.append(_ref("observed_git", target=sha, source_run_id=hit.get("run_id"), seq=hit.get("seq"),
                                     call_id=hit.get("call_id"), result="observed_in_git_output", at=hit.get("at")))
                continue
            found = False
            for root in git_roots:
                if await _commit_exists(root, sha):
                    found = True
                    verified.append(_ref("observed_git", target=sha, result="exists_in_repository", scope=str(root)))
                    break
            if found:
                continue
            if git_roots and not partial:
                contradictions.append({"code": "commit_not_found", "detail": f"커밋 {sha} 이(가) 저장소에도 Run 의 git 출력에도 없다"})
            elif git_roots:
                unverified.append({"code": "commit_unverifiable", "detail": f"커밋 {sha}: 오래된 Run 기록을 다 보지 못해 대조 불가"})
            else:
                unverified.append({"code": "commit_unverifiable", "detail": f"커밋 {sha}: 확인할 git 저장소 없음"})
        if _TEST_CLAIM_RE.search(sent) and _FAIL_REPORT_RE.search(sent):
            # 실패를 함께 보고한 문장은 성공 주장이 아니다. 정직한 `94 passed, 1 failed` 를 모순으로 거부하지 않는다.
            unverified.append({"code": "declared_failure", "detail": f"실패를 함께 보고한 문장: {raw[:80]}"})
        elif _TEST_CLAIM_RE.search(sent):
            counts = [int(x) for x in _PASS_COUNT_RE.findall(sent)]  # 앞에 pytest·테스트가 오면 정규식이 건수 그룹을 놓친다
            family = _test_family(sent)
            scope = [t for t in decided if family and t["target"].startswith(family)] or decided
            if not tests:
                if have_obs and not partial:
                    contradictions.append({"code": "test_not_run", "detail": f"테스트 성공을 주장하지만 관측된 테스트 실행이 없다: {raw[:80]}"})
                else:
                    unverified.append({"code": "test_unobservable", "detail": "관측된 도구 기록이 없어 테스트 주장을 대조하지 못함"})
            elif not decided:
                unverified.append({"code": "test_outcome_unknown", "detail": "테스트를 실행했으나 결과 요약을 읽지 못함"})
            else:
                last = scope[-1]
                allowed = {t["passed"] for t in decided if t["outcome"] == "passed" and t.get("passed") is not None}
                same_run = [t for t in decided if t["run_id"] == last["run_id"] and t["outcome"] == "passed"
                            and t.get("passed") is not None]
                if same_run:
                    allowed.add(sum(t["passed"] for t in same_run))
                latest: dict[str, int] = {}  # 대상별 가장 최근 통과 건수의 합: 파일을 나눠 돌린 결과를 합쳐 보고한 경우
                for t in decided:
                    if t["outcome"] == "passed" and t.get("passed") is not None:
                        latest[t["target"]] = t["passed"]
                    elif t["outcome"] == "failed":
                        latest.pop(t["target"], None)
                if latest:
                    allowed.add(sum(latest.values()))
                if last["outcome"] == "failed":
                    contradictions.append({"code": "test_failed", "detail":
                                           f"테스트 성공을 주장하지만 가장 최근 관측 테스트가 실패했다: {last['target']} {last.get('line') or ''}".strip()})
                elif (bad := next((n for n in counts if allowed and n not in allowed), None)) is not None:
                    contradictions.append({"code": "test_count_mismatch", "detail":
                                           f"통과 {bad}건을 주장하지만 관측된 통과 건수는 {sorted(allowed)}"})
                else:
                    verified.append(_ref("observed_test", target=last["target"], source_run_id=last["run_id"], seq=last.get("seq"),
                                         call_id=last.get("call_id"), result=last.get("line") or "passed", at=last.get("at")))
        if _CHANGE_VERB_RE.search(sent) and not _NOT_CHANGE_RE.search(sent):
            for pm in _PATH_RE.finditer(sent):
                path = pm.group(1)
                hit = next((v for p, v in {**pool["writes"], **pool["files"]}.items() if p == path or p.endswith("/" + path.lstrip("./"))
                            or path.endswith("/" + p.lstrip("./"))), None)
                if hit:
                    verified.append(_ref("observed_file", target=path, source_run_id=hit.get("run_id"), seq=hit.get("seq"),
                                         result="changed_in_run"))
                    continue
                exists = False
                for root in roots:
                    if root.is_dir() and await _path_exists(root, path):
                        exists = True
                        break
                if exists:
                    unverified.append({"code": "file_change_unobserved", "detail": f"{path}: 존재하지만 이번 관측에서 변경은 보지 못함"})
                elif have_obs and not partial and any(r.is_dir() for r in roots):
                    contradictions.append({"code": "file_not_found", "detail": f"{path} 수정을 주장하지만 파일이 존재하지 않고 변경 관측도 없다"})
                else:
                    unverified.append({"code": "file_unverifiable", "detail": f"{path}: 확인할 작업 공간 기록 없음"})
        if _DEPLOY_CLAIM_RE.search(sent) and not _NOT_DEPLOYED_RE.search(sent):
            deploys = pool["deploys"]
            if not deploys:
                unverified.append({"code": "deploy_unobserved", "detail": "관측된 배포 명령이 없어 배포 주장을 대조하지 못함"})
            else:
                last_bad = max((i for i, d in enumerate(deploys) if not d["ok"] and d["kind"] == "deploy"), default=None)
                later_ok = last_bad is not None and any(d["ok"] for d in deploys[last_bad + 1:])
                if last_bad is not None and not later_ok:
                    d = deploys[last_bad]
                    contradictions.append({"code": "deploy_failed", "detail":
                                           f"배포 성공을 주장하지만 관측된 마지막 배포 명령이 실패했고 뒤에 성공·확인이 없다: {d['cmd']}"})
                else:
                    ok = next((d for d in reversed(deploys) if d["ok"]), None)
                    if ok:
                        verified.append(_ref("observed_external", target=ok["cmd"], source_run_id=ok["run_id"], seq=ok.get("seq"),
                                             call_id=ok.get("call_id"), result="ok", at=ok.get("at")))
    return {"contradictions": contradictions[:8], "unverified": unverified[:8], "verified": verified[:10]}


def evidence_level(result: dict[str, Any]) -> str:
    """contradicted | observed | unverified | self_claim."""
    if result["contradictions"]:
        return "contradicted"
    if result["verified"]:
        return "observed"
    return "unverified" if result["unverified"] else "self_claim"


# ---- 진전 판정 -------------------------------------------------------------------------------------------
def new_ledger() -> dict[str, Any]:
    return {"seen": [], "blocked": [], "tests": {}, "weak_streak": 0, "declared": [], "req_rank": {}}


def assess_progress(obs: dict[str, Any], ledger: dict[str, Any] | None) -> dict[str, Any]:
    """관측된 사실이 이 목표에서 처음인가. 직전 Run 과만 비교하지 않고 목표 전체의 이력(ledger)과 비교한다.

    진전으로 세는 것: 새 실질 변경(diff 지문), 새 파일, 성공한 커밋·푸시, 통과한 테스트(이전 실패 뒤의 통과 포함).
    단독으로 세지 않는 것: 호출 수, 실패한 쓰기·커밋, 이미 본 사실의 반복, 이전에 본 변경을 되돌리는 변경(수정·원복),
    같은 실패 원인의 반복. 조사(새 대상 조회)와 새 테스트 실패 원인은 약한 진전이라 연속 MAX_WEAK_STREAK 회까지만 인정한다.
    반환의 ledger 를 저장해야 이력이 이어진다."""
    ledger = {**new_ledger(), **(ledger or {})}
    seen, blocked = set(ledger["seen"]), set(ledger["blocked"])
    seen_order: list[str] = list(ledger["seen"])
    blocked_order: list[str] = list(ledger["blocked"])
    test_state: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (ledger.get("tests") or {}).items()}
    new: list[dict[str, Any]] = []
    repeated: Counter[str] = Counter()

    def take(key: str, cat: str, inv: str | None = None) -> bool:
        if key in blocked:
            repeated["reverted"] += 1
            return False
        if key in seen:
            repeated[cat] += 1
            return False
        seen.add(key)
        seen_order.append(key)
        if inv and inv != key and inv not in blocked:
            blocked.add(inv)
            blocked_order.append(inv)
        new.append({"k": key, "cat": cat})
        return True

    for f in obs.get("facts") or []:
        take(f["k"], f["cat"], f.get("inv"))
    for t in obs.get("tests") or []:
        tgt = t.get("target") or ""
        if t["outcome"] == "failed":
            take(f"tf:{_h(tgt)}:{t.get('sig') or ''}", "test_fail")
            test_state[tgt] = {"last": "failed", "sig": t.get("sig") or ""}
        elif t["outcome"] == "passed":
            st = test_state.get(tgt) or {}
            prior = st.get("sig") if st.get("last") == "failed" else None
            take(f"tp:{_h(tgt)}:{prior or 'first'}", "test_pass")
            test_state[tgt] = {"last": "passed", "sig": ""}
    for cat, n in (obs.get("failed") or {}).items():
        if cat != "test":
            repeated[f"failed_{cat}"] += int(n)
    substantive = [f for f in new if f["cat"] not in ("inspect", "test_fail")]
    weak = [f for f in new if f["cat"] in ("inspect", "test_fail")]
    streak = int(ledger.get("weak_streak") or 0)
    if substantive:
        progressed, streak = True, 0
    elif weak:
        streak += 1
        progressed = streak <= MAX_WEAK_STREAK
        if not progressed:
            repeated["weak_only_streak"] += 1
    else:
        progressed = False
    out = {**ledger, "seen": seen_order[-LEDGER_CAP:], "blocked": blocked_order[-LEDGER_CAP:], "tests": test_state,
           "weak_streak": streak}
    return {"progressed": progressed, "new": [f["k"] for f in new], "new_categories": sorted({f["cat"] for f in new}),
            "repeated": dict(repeated), "ledger": out}


def legacy_summary(obs: dict[str, Any]) -> dict[str, Any]:
    """기존 run_evidence 호출자(`evidence_line`, 상태 복원)가 쓰는 모양. 도구 호출이 아니라 성공한 관측만 센다."""
    files = sorted({f["path"] for f in obs["files"]} | set(obs["new_files"])
                   | {w["path"] for w in obs["writes"] if w.get("path")})
    commits = sum(1 for c in obs["commits"])
    writes = len(obs["writes"]) + int(obs.get("shell_writes") or 0)
    tests = [t["line"] for t in obs["tests"] if t["outcome"] != "unknown" and t.get("line")][-3:]
    effective = bool(files or commits or writes or tests)
    return {"tool_calls": obs["calls"], "write_calls": writes, "commits": commits, "files": files[:30],
            "file_count": len(files), "tests": tests, "effective": effective,
            "failed": obs.get("failed") or {}, "unresolved_calls": obs.get("unresolved_calls", 0),
            "fingerprint": _h("|".join(f["k"] for f in obs["facts"]), 12), "observed": obs}
