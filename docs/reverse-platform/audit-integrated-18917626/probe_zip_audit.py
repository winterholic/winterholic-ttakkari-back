"""Independent, DB-free adversarial probe for fixed 18917626 ZIP/prompt behavior."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

from ttakkari import reverse_tasks as tasks
from ttakkari.reverse_web_evidence import verify_web_capture

BASE = Path(__file__).resolve().parents[3]
SHA = "18917626934ab38302bddfee1c7f4b9ebe6dde73"
AUDIT_PREFIX = "docs/reverse-platform/audit-integrated-18917626/"
SCRIPT_NAMES = ("reverse_web_evidence_capture.py", "reverse_web_evidence_verify.py")


def png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    width, height = VIEWPORT["width"], VIEWPORT["height"]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def screenshot(path: str, blob: bytes) -> dict:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    return {"path": path, "sha256": digest(blob), "size_bytes": len(blob),
            "viewport": VIEWPORT, "format": "png"}


def observations(run_id: str, comparison_id: str) -> dict:
    input_data = {"steps": [{"action": "click", "target": "button#go"}]}
    env = {"browser": "independent-fixture", "os": "audit"}
    state = {"screen": "ready"}
    events = [{"type": "click", "target": "button#go"}]
    shared = {"run_id": run_id, "input": input_data, "viewport": VIEWPORT,
              "pre_state": state, "environment": env, "events": events,
              "dom": [{"tag": "button", "text": "Go"}], "state": {"screen": "done"},
              "network": [], "complete": True, "capture_state": "complete"}
    original = {**shared, "observed_at": "2026-11-07T00:00:00Z", "url": "https://example.test/original",
                "screenshot": screenshot(".reverse/web-captures/shared/original.png", png((220, 20, 20)))}
    replica = {**shared, "observed_at": "2026-11-07T00:00:01Z", "url": "https://example.test/replica",
               "screenshot": screenshot(".reverse/web-captures/shared/replica.png", png((20, 20, 220)))}
    return {"comparison_id": comparison_id, "behavior": "button changes screen", "input": input_data,
            "original": original, "replica": replica, "run_id": run_id,
            "expected_viewport": VIEWPORT, "origin_map": {"https://example.test/original": "https://example.test/replica"},
            "environment": env, "pre_state": state, "verified_at": "2026-11-07T00:00:02Z"}


def assert_baseline() -> str:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BASE, text=True).strip()
    if subprocess.run(["git", "merge-base", "--is-ancestor", SHA, head], cwd=BASE, check=False).returncode:
        raise AssertionError(f"fixed product baseline {SHA} is not an ancestor of HEAD {head}")
    changed = subprocess.check_output(["git", "diff", "--name-only", SHA, head], cwd=BASE, text=True).splitlines()
    outside = [path for path in changed if not path.startswith(AUDIT_PREFIX)]
    if outside:
        raise AssertionError(f"HEAD differs from fixed product baseline outside audit path: {outside}")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=BASE, text=True)
    outside_dirty = [line for line in dirty.splitlines() if AUDIT_PREFIX not in line]
    if outside_dirty:
        raise AssertionError(f"worktree has changes outside audit path: {outside_dirty}")
    return head


def prompt_commands(job_root: Path, run_id: str) -> tuple[str, str, str]:
    job = SimpleNamespace(output_path=str(job_root), target="test", result={
        "schema_version": 2, "job": {"mode": "reconstruct", "target_type": "web", "title": "audit"}})
    run = SimpleNamespace(id=run_id, session_id=f"session-{run_id}")
    text = tasks._prompt(job, run)
    commands = [line.strip() for line in text.splitlines()
                if line.startswith("cd ") and "&& PYTHONPATH=" in line]
    if len(commands) != 2:
        raise AssertionError(f"expected generated capture/verify commands, got {len(commands)}")
    return text, commands[0], commands[1]


def execute_prompt_command(command: str, *, expected: int = 0) -> dict:
    completed = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=15, check=False)
    outcome = {"command": command, "exit": completed.returncode,
               "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    if completed.returncode != expected:
        raise AssertionError(json.dumps(outcome, ensure_ascii=False))
    return outcome


def zip_round_trip() -> dict:
    roles = []
    role_inputs = [("implementation", "impl-audit-run", "impl-audit-session", "impl-case"),
                   ("verifier", "verify-audit-run", "verify-audit-session", "verify-case")]
    cli = []
    for role, run_id, session_id, comparison_id in role_inputs:
        _, capture_cmd, verify_cmd = prompt_commands(ROOT, run_id)
        spec = observations(run_id, comparison_id)
        write_json(ROOT / ".reverse/input/web-observations.json", spec)
        capture = execute_prompt_command(capture_cmd)
        verify_cmd = verify_cmd.replace("<comparison-id>", comparison_id)
        verify = execute_prompt_command(verify_cmd, expected=0)
        report_path = ROOT / ".reverse/comparisons" / f"{comparison_id}-{run_id}.web.json"
        record = json.loads(report_path.read_text(encoding="utf-8"))
        check_log = f"{role} role check log\n".encode()
        check_rel = f"evidence/roles/{role}/checks/000-check.log"
        source_log = ROOT / check_rel
        source_log.parent.mkdir(parents=True, exist_ok=True)
        source_log.write_bytes(check_log)
        roles.append({"role": role, "run_id": run_id, "session_id": session_id,
                      "report": {"comparisons": [record], "checks": [{"command": "capture + verify",
                          "exit_code": 0, "expected_exit_code": 0, "log": check_rel}]}})
        cli.append({"role": role, "capture": capture, "verify": verify})

    outbox = TMP / "zip"
    outbox.mkdir()
    (ROOT / "README.md").write_text("audit source\n", encoding="utf-8")
    (ROOT / "src/main.py").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "src/main.py").write_text("print('audit')\n", encoding="utf-8")
    base_report = {"source_files": ["src/main.py"], "checks": []}
    path = tasks.bundle(ROOT, outbox, base_report, evidence_roles=roles)
    extraction = TMP / "extracted"
    extraction.mkdir()
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise AssertionError(f"ZIP CRC failed: {bad_member}")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise AssertionError("duplicate ZIP member")
        archive.extractall(extraction)
    manifest = json.loads((extraction / "reverse-web-evidence-manifest.json").read_text())
    by_role = {item["role"]: item for item in manifest["runs"]}
    comparisons = {}
    recompute = []
    checks = []
    inventories = {}
    shared_png_paths = set()
    for role, run_id, session_id, comparison_id in role_inputs:
        role_manifest = by_role[role]
        comparison = role_manifest["comparisons"][0]
        if (role_manifest["run_id"], role_manifest["session_id"]) != (run_id, session_id):
            raise AssertionError(f"role linkage lost for {role}: {role_manifest}")
        if not comparison["included"] or comparison["integrity"]["status"] != "verified":
            raise AssertionError(f"valid comparison refused for {role}: {comparison}")
        record_member = next(f["path"] for f in comparison["files"] if f.get("kind") == "comparison")
        record = json.loads((extraction / record_member).read_text())
        checked = verify_web_capture(extraction, record, expected_run_id=run_id)
        if checked["evidence"][0]["integrity"]["status"] != "verified":
            raise AssertionError(f"extracted comparison no longer verifies: {checked}")
        recompute.append({"role": role, "run_id": checked["run_id"], "result": checked["result"],
                          "functional": checked["evidence"][0]["functional_result"],
                          "visual": checked["evidence"][0]["visual_result"],
                          "integrity": checked["evidence"][0]["integrity"]["status"]})
        raw_ref = record["evidence"][0]["capture"]
        raw_blob = (extraction / raw_ref["path"]).read_bytes()
        if digest(raw_blob) != raw_ref["sha256"] or len(raw_blob) != raw_ref["size_bytes"]:
            raise AssertionError(f"raw capture archive hash mismatch: {raw_ref['path']}")
        raw = json.loads(raw_blob)
        if raw["run_id"] != run_id or raw["comparison_id"] != comparison_id:
            raise AssertionError(f"raw role identity mismatch: {raw['run_id']} {raw['comparison_id']}")
        for side in ("original", "replica"):
            image = raw[side]["screenshot"]
            image_blob = (extraction / image["path"]).read_bytes()
            if digest(image_blob) != image["sha256"] or len(image_blob) != image["size_bytes"]:
                raise AssertionError(f"PNG archive hash mismatch for {role}/{side}")
            shared_png_paths.add(image["path"])
        check_entry = role_manifest["execution_checks"][0]
        log_blob = (extraction / check_entry["archive_path"]).read_bytes()
        if digest(log_blob) != check_entry["sha256"] or len(log_blob) != check_entry["size_bytes"]:
            raise AssertionError(f"role log hash mismatch for {role}")
        checks.append({"role": role, "path": check_entry["archive_path"],
                       "sha256": digest(log_blob), "size_bytes": len(log_blob), "run_id": run_id})
        inventories[role] = {"run_id": run_id, "session_id": session_id,
            "evidence_files": [{"path": item["path"], "kind": item.get("kind", "raw_or_png"),
                                "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
                               for item in comparison["files"]],
            "check_log": {"path": check_entry["archive_path"], "sha256": check_entry["sha256"],
                          "size_bytes": check_entry["size_bytes"]}}
        comparisons[role] = comparison
    if len(shared_png_paths) != 2:
        raise AssertionError(f"same relative screenshot paths did not de-duplicate as expected: {shared_png_paths}")
    for item in manifest["runs"]:
        for comparison in item["comparisons"]:
            for record in comparison["files"]:
                blob = (extraction / record["path"]).read_bytes()
                if digest(blob) != record["sha256"] or len(blob) != record["size_bytes"]:
                    raise AssertionError(f"manifest file SHA/size mismatch: {record['path']}")
    # Tamper after a successful verification/archive, then require the next bundle to reject it.
    wrong_role = {**roles[1], "run_id": "wrong-run-id"}
    wrong_outbox = TMP / "wrong-run-outbox"
    wrong_outbox.mkdir()
    wrong_zip = tasks.bundle(ROOT, wrong_outbox, base_report, evidence_roles=[wrong_role])
    with zipfile.ZipFile(wrong_zip) as archive:
        wrong_manifest = json.loads(archive.read("reverse-web-evidence-manifest.json"))
        wrong_comparison = wrong_manifest["runs"][0]["comparisons"][0]
        if wrong_comparison["included"] or "does not match" not in (wrong_comparison.get("reason") or ""):
            raise AssertionError(f"wrong Run ID was not refused: {wrong_comparison}")
    impl_raw = json.loads((ROOT / ".reverse/comparisons/impl-case-impl-audit-run.web.json").read_text())
    impl_png = ROOT / impl_raw["original"]["screenshot"]["path"]
    impl_png.write_bytes(impl_png.read_bytes() + b"post-verification-mutation")
    tampered_outbox = TMP / "tampered-outbox"
    tampered_outbox.mkdir()
    tampered_zip = tasks.bundle(ROOT, tampered_outbox, base_report, evidence_roles=[roles[0]])
    with zipfile.ZipFile(tampered_zip) as archive:
        tamper_manifest = json.loads(archive.read("reverse-web-evidence-manifest.json"))
        tampered = tamper_manifest["runs"][0]["comparisons"][0]
        if tampered["included"] or not tampered.get("reason"):
            raise AssertionError(f"post-capture PNG mutation was not refused: {tampered}")
    return {"zip": str(path), "members": names, "roles": [
        {"role": role, "run_id": run_id, "session_id": session_id,
         "included": comparisons[role]["included"], "png_paths": sorted(shared_png_paths)}
        for role, run_id, session_id, _ in role_inputs],
        "recomputed": recompute, "logs": checks, "inventory": inventories, "cli": cli,
        "wrong_run_id": {"included": wrong_comparison["included"], "reason": wrong_comparison["reason"]},
        "post_capture_png_tamper": {"included": tampered["included"], "reason": tampered["reason"]}}


def adversarial_files() -> dict:
    root = TMP / "hostile-root"
    root.mkdir()
    external = TMP / "outside.txt"
    external.write_text("outside\n")
    (root / "linked.txt").symlink_to(external)
    (root / ".env").write_text("API_KEY=probe-only\n")
    (root / "exact-limit.txt").write_bytes(b"x" * tasks.MAX_REPORT)
    (root / "over-limit.txt").write_bytes(b"x" * (tasks.MAX_REPORT + 1))
    (root / "directory").mkdir()
    names = ["../outside.txt", str(external), "linked.txt", ".env", "exact-limit.txt",
             "over-limit.txt", "directory", "src/main.py"]
    (root / "src").mkdir()
    (root / "src/main.py").write_text("ok\n")
    outbox = TMP / "hostile-outbox"
    outbox.mkdir()
    path = tasks.bundle(root, outbox, {"source_files": names}, evidence_roles=[])
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("reverse-web-evidence-manifest.json"))
        files = {item["path"]: item for item in manifest["files"]}
        expected_rejected = ["../outside.txt", str(external), "linked.txt", ".env", "over-limit.txt", "directory"]
        for name in expected_rejected:
            if files[name]["included"]:
                raise AssertionError(f"unsafe file unexpectedly included: {name}")
        if not files["exact-limit.txt"]["included"]:
            raise AssertionError(f"file at MAX_REPORT rejected: {files['exact-limit.txt']}")
        if "src/main.py" not in archive.namelist():
            raise AssertionError("normal source omitted")
        return {"rejected": {name: files[name]["reason"] for name in expected_rejected},
                "exact_limit_included": True, "over_limit_included": files["over-limit.txt"]["included"],
                "normal_source_included": True}


def fifo_timeout_probe() -> dict:
    root = TMP / "fifo-root"
    root.mkdir()
    run_id, comparison_id = "fifo-web-run", "fifo-web-case"
    original_root = ROOT
    globals()["ROOT"] = root
    spec = observations(run_id, comparison_id)
    globals()["ROOT"] = original_root
    write_json(root / ".reverse/input/web-observations.json", spec)
    job = SimpleNamespace(output_path=str(root), target="fifo", result={
        "schema_version": 2, "job": {"mode": "reconstruct", "target_type": "web"}})
    run = SimpleNamespace(id=run_id, session_id="fifo-web-session")
    _, capture_cmd, _ = prompt_commands(root, run_id)
    capture = execute_prompt_command(capture_cmd)
    report_path = root / ".reverse/comparisons" / f"{comparison_id}-{run_id}.web.json"
    record = json.loads(report_path.read_text(encoding="utf-8"))
    run_entry = {"role": "implementation", "run_id": run_id, "session_id": "fifo-web-session",
                 "report": {"comparisons": [record], "checks": []}}

    cases: dict[str, dict] = {}

    def invoke(case: str, report: dict, roles: list[dict], expected_key: str) -> dict:
        out = TMP / f"fifo-out-{case}"
        out.mkdir()
        spec_path = TMP / f"fifo-spec-{case}.json"
        write_json(spec_path, {"root": str(root), "out": str(out), "report": report, "roles": roles,
                              "expected_key": expected_key})
        code = ("import json,sys,zipfile; from pathlib import Path; "
                "from ttakkari.reverse_tasks import bundle; s=json.loads(Path(sys.argv[1]).read_text()); "
                "p=bundle(Path(s['root']),Path(s['out']),s['report'],evidence_roles=s['roles']); "
                "z=zipfile.ZipFile(p); m=json.loads(z.read('reverse-web-evidence-manifest.json')); "
                "k=s['expected_key']; result=(next(x for x in m['files'] if x['path']=='source.fifo') if k=='source' else "
                "m['runs'][0]['execution_checks'][0] if k=='check' else m['runs'][0]['comparisons'][0]); "
                "print(json.dumps({'included':result.get('included'), 'reason':result.get('reason')}))")
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(BASE / "src")
        try:
            result = subprocess.run([sys.executable, "-c", code, str(spec_path)], cwd=BASE, env=child_env,
                                    text=True, capture_output=True, timeout=3, check=False)
            output = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
            if result.returncode != 0 or output.get("included") is not False or not output.get("reason"):
                raise AssertionError(f"{case}: exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}")
            return {"exit": result.returncode, "timed_out": False, **output,
                    "reason_mentions_nonregular": ("regular file" in output["reason"].lower()
                        or "일반 파일이 아닙니다" in output["reason"])}
        except subprocess.TimeoutExpired as exc:
            return {"exit": None, "timed_out": True,
                    "stdout": (exc.stdout or b"").decode(errors="replace"),
                    "stderr": (exc.stderr or b"").decode(errors="replace")}

    # Source and check-log entry points pass through the ZIP descriptor reader.
    os.mkfifo(root / "source.fifo")
    cases["source"] = invoke("source", {"source_files": ["source.fifo"], "checks": []}, [], "source")
    os.mkfifo(root / "check.fifo")
    check_entry = {**run_entry, "report": {"comparisons": [], "checks": [{"command": "fifo", "log": "check.fifo"}]}}
    # The source manifest also contains README, so select the first check explicitly.
    cases["check_log"] = invoke("check", {"source_files": [], "checks": []}, [check_entry], "check")

    # Raw capture and PNG are checked by the web verifier before archiving.
    raw_path = root / record["evidence"][0]["capture"]["path"]
    raw_blob = raw_path.read_bytes()
    raw_path.unlink()
    os.mkfifo(raw_path)
    cases["web_raw_json"] = invoke("web-raw", {"source_files": [], "checks": []}, [run_entry], "comparison")
    raw_path.unlink()
    raw_path.write_bytes(raw_blob)
    original_png = root / record["original"]["screenshot"]["path"]
    png_blob = original_png.read_bytes()
    original_png.unlink()
    os.mkfifo(original_png)
    cases["web_png"] = invoke("web-png", {"source_files": [], "checks": []}, [run_entry], "comparison")
    original_png.unlink()
    original_png.write_bytes(png_blob)
    return {"timeout_seconds_per_subprocess": 3, "capture_cli": capture, "cases": cases,
            "all_returned_reason_without_timeout": all(not item["timed_out"] and item.get("reason")
                                                        for item in cases.values())}


async def collect_fake() -> dict:
    root = TMP / "collect-root"
    root.mkdir()
    base = {"ready": True, "schema_version": 2, "job": {"mode": "reconstruct", "target_type": "web"}}
    impl_report = {**base, "comparisons": [{"id": "implementation-latest"}]}
    verifier_report = {**base, "comparisons": [{"id": "verifier-current"}]}
    job = SimpleNamespace(output_path=str(root), result={"schema_version": 2,
        "job": {"mode": "reconstruct", "target_type": "web"},
        "latest": {"run_id": "impl-run", "report": impl_report, "verification_run_id": "verify-run",
                   "verification_report": verifier_report}}, session_id="impl-session",
        verifier_session_id="verify-session")

    class FakeDb:
        async def get(self, _model, _id):
            return job

    previous_read, previous_bundle = tasks.read_report, tasks.bundle
    captured: dict = {}
    tasks.read_report = lambda *_a, **_k: {**verifier_report}
    tasks.bundle = lambda *_a, **kw: captured.update(kw)
    run = SimpleNamespace(id="verify-run", session_id="verify-session",
                          usage={"reverse": {"job_id": "00000000-0000-0000-0000-000000000001", "role": "verify"}})
    try:
        await tasks.collect_outputs(FakeDb(), run, TMP / "fake-outbox")
    finally:
        tasks.read_report, tasks.bundle = previous_read, previous_bundle
    roles = captured["evidence_roles"]
    got = {item["role"]: {"run_id": item["run_id"], "session_id": item["session_id"],
                           "comparison_ids": [c["id"] for c in item["report"]["comparisons"]]}
           for item in roles}
    if got.get("implementation", {}).get("comparison_ids") != ["implementation-latest"]:
        raise AssertionError(f"latest implementation report lost: {got}")
    if got.get("verifier", {}).get("comparison_ids") != ["verifier-current"]:
        raise AssertionError(f"current verifier report lost: {got}")
    return got


def prompt_layout_probe() -> dict:
    original_file = tasks.__file__
    original_executable = tasks.sys.executable
    job_root = TMP / "Prompt Job With Spaces"
    try:
        tasks.__file__ = str(BASE / "src/ttakkari/reverse_tasks.py")
        dev_text, dev_capture, dev_verify = prompt_commands(job_root, "dev-layout-run")
        job_root.mkdir(parents=True, exist_ok=True)
        dev_interpreter = str(BASE.parent / "back/.venv/bin/python")
        dev_interpreter = str(Path(sys.executable))
        if str(Path(sys.executable)) not in dev_capture or str(BASE / "scripts/reverse_web_evidence_capture.py") not in dev_capture:
            raise AssertionError("development prompt paths do not point at checkout/../back/.venv")

        mock_root = TMP / "mock-deploy" / "releases" / SHA
        tmp_root = TMP.resolve()
        mock_root_resolved = mock_root.resolve()
        if not mock_root_resolved.is_relative_to(tmp_root) or mock_root.is_symlink():
            raise AssertionError(f"mock release write target escapes tmp or is a symlink: {mock_root}")
        if mock_root.exists():
            raise AssertionError(f"mock release target already exists: {mock_root}")
        (mock_root / "src").mkdir(parents=True)
        shutil.copytree(BASE / "src/ttakkari", mock_root / "src/ttakkari")
        (mock_root / "scripts").mkdir()
        for name in SCRIPT_NAMES:
            (mock_root / "scripts" / name).write_bytes((BASE / "scripts" / name).read_bytes())
        venv_target = mock_root / ".venv"
        venv_target_resolved = venv_target.resolve()
        if not venv_target_resolved.is_relative_to(tmp_root) or venv_target.is_symlink():
            raise AssertionError(f"mock release venv write target escapes tmp or is a symlink: {venv_target}")
        shutil.copytree(BASE.parent / "back/.venv", venv_target)
        if venv_target.is_symlink() or not venv_target.is_dir():
            raise AssertionError(f"mock release venv was not copied as a directory: {venv_target}")
        if not Path(venv_target / "bin/python").resolve().is_relative_to(tmp_root):
            raise AssertionError(f"mock release interpreter resolves outside tmp: {venv_target / 'bin/python'}")
        tasks.__file__ = str(mock_root / "src/ttakkari/reverse_tasks.py")
        expected = str(mock_root / ".venv/bin/python")
        tasks.sys.executable = expected
        release_commands = []
        for role, run_id, comparison_id in (("implementation", "release-impl-run", "release-impl-case"),
                                             ("verifier", "release-verifier-run", "release-verifier-case")):
            _deploy_text, deploy_capture, deploy_verify = prompt_commands(job_root, run_id)
            original_root = ROOT
            globals()["ROOT"] = job_root
            spec = observations(run_id, comparison_id)
            globals()["ROOT"] = original_root
            write_json(job_root / ".reverse/input/web-observations.json", spec)
            deploy_verify = deploy_verify.replace("<comparison-id>", comparison_id)
            capture_result = execute_prompt_command(deploy_capture)
            verify_result = execute_prompt_command(deploy_verify)
            release_commands.append({"role": role, "capture": capture_result, "verify": verify_result,
                "python": expected, "capture_script": str(mock_root / "scripts/reverse_web_evidence_capture.py"),
                "verify_script": str(mock_root / "scripts/reverse_web_evidence_verify.py")})
        observed = {"commands": release_commands, "all_exit_zero": all(
                       row[step]["exit"] == 0 for row in release_commands for step in ("capture", "verify")),
                    "release_venv_exists": Path(expected).exists(),
                    "mock_release_root_resolved_inside_tmp": mock_root.resolve().is_relative_to(tmp_root),
                    "mock_release_root_is_symlink": mock_root.is_symlink(),
                    "mock_release_venv_is_symlink": venv_target.is_symlink(),
                    "mock_release_interpreter_resolved_inside_tmp": Path(expected).resolve().is_relative_to(tmp_root),
                    "mock_release_capture_script_exists": (mock_root / "scripts/reverse_web_evidence_capture.py").is_file(),
                    "mock_release_verify_script_exists": (mock_root / "scripts/reverse_web_evidence_verify.py").is_file()}
        return {"development": {"command": dev_capture, "interpreter": dev_interpreter,
                                 "quoted_space_path": shlex.quote(str(job_root)) in dev_capture,
                                 "has_verify_command": "reverse_web_evidence_verify.py" in dev_verify,
                                 "prompt_mentions_both_roles": all(x in dev_text for x in ("구현자", "검증자"))},
                "mock_release": {"release_root": str(mock_root), "expected_venv": expected,
                                 "execution": observed}}
    finally:
        tasks.__file__ = original_file
        tasks.sys.executable = original_executable


def main() -> None:
    global TMP, ROOT, VIEWPORT
    head = assert_baseline()
    TMP = Path(tempfile.mkdtemp(prefix="Reverse Zip Audit "))
    ROOT = TMP / "Reverse Web Job With Spaces"
    ROOT.mkdir()
    VIEWPORT = {"width": 390, "height": 844, "device_scale_factor": 1}
    layout = prompt_layout_probe()
    bundle_results = zip_round_trip()
    hostile = adversarial_files()
    fifo = fifo_timeout_probe()
    fake = asyncio.run(collect_fake())
    result = {"baseline": head, "tmp": str(TMP), "prompt_paths": layout, "two_role_zip": bundle_results,
              "hostile_files": hostile, "fifo_hang": fifo, "fake_collect_outputs": fake,
              "ordering": {"manager_collect_outputs_line": 1783, "manager_on_finished_line": 2159,
                           "note": "collect_outputs runs before _finish invokes on_finished; current report is read directly, stored latest report is role-joined"}}
    result_path = BASE / "docs/reverse-platform/audit-integrated-18917626/probe-zip-output.json"
    write_json(result_path, result)
    print(json.dumps({"baseline": head, "result_file": str(result_path), "tmp": str(TMP),
                      "release_all_exit_zero": layout["mock_release"]["execution"]["all_exit_zero"],
                      "fifo_timed_out": any(item["timed_out"] for item in fifo["cases"].values()),
                      "fifo_all_rejected": fifo["all_returned_reason_without_timeout"],
                      "roles": fake}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
