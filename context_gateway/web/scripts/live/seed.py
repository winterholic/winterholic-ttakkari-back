"""실제 Gateway(control)에 합성 스냅샷을 발행하고 1회용 키를 새로 발급해 키 파일(JSON, 0600)에 쓴다.

키는 1회용이라 테스트/브라우저 검증 전에 매번 다시 실행한다. 출력에는 키를 찍지 않는다.
사용: python3 scripts/live/seed.py [keys.json 경로]   (기본 /tmp/ctx-gw-live/keys.json)
"""
import json
import os
import sys
import urllib.request

CONTROL = os.environ.get("CONTEXT_GATEWAY_CONTROL_URL", "http://127.0.0.1:18789")
HEADERS = {"Authorization": "Bearer synthetic-control-token-not-secret", "Content-Type": "application/json"}
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ctx-gw-live/keys.json"


def post(path, body):
    request = urllib.request.Request(CONTROL + path, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    return json.load(urllib.request.urlopen(request))


def publish(slug, name, revision, documents, verified_at="2026-11-16T00:05:00Z"):
    return post(
        "/control/v1/snapshots",
        {
            "project_slug": slug,
            "project_name": name,
            "project_description": f"합성 프로젝트 {name}",
            "source_refs": [f"opaque-ref-{slug}", "opaque-ref-2"],
            "source_revision": revision,
            "generated_at": "2026-11-16T00:00:00Z",
            "verified_at": verified_at,
            "documents": documents,
        },
    )


HOSTILE = (
    "# 개요\n\n합성 **데이터**다.\n\n<script>window.__pwned = 1</script>\n\n"
    '<img src=x onerror="window.__pwned=1">\n\n[javascript 링크](javascript:alert(1)) [정상 링크](https://example.com/a)\n\n'
    "![외부 이미지](https://tracker.invalid/p.png)\n\n| 열 | 값 |\n|---|---|\n| a | 1 |\n\n```\ncode block\n```\n"
)
publish(
    "synthetic-alpha", "Synthetic Alpha", "rev-1",
    [
        {"id": "overview", "title": "Approved overview", "content_type": "text/markdown", "content": HOSTILE},
        {"id": "notes", "title": "Plain notes", "content_type": "text/plain", "content": "plain text 알파\n둘째 줄"},
    ],
)
alpha = publish(
    "synthetic-alpha", "Synthetic Alpha", "rev-2",
    [
        {"id": "overview", "title": "Approved overview", "content_type": "text/markdown", "content": "# 개요 v2\n\n두 번째 스냅샷. 알파 검색어.\n"},
        {"id": "config", "title": "Config", "content_type": "application/json", "content": '{"a": 1}'},
    ],
)
beta = publish("synthetic-beta", "Synthetic Beta", "rev-b", [{"id": "secret", "title": "Beta only", "content_type": "text/markdown", "content": "BETA-ONLY-TEXT"}])
A, B = alpha["project_id"], beta["project_id"]
FULL = ["read", "search", "export"]


def key(label, projects):
    return post("/control/v1/keys", {"label": label, "projects": projects})["key"]


keys = {
    "alpha": key("alpha", {A: {"scopes": FULL}}),
    "alpha2": key("alpha2", {A: {"scopes": FULL}}),
    "alpha3": key("alpha3", {A: {"scopes": FULL}}),
    "readonly": key("readonly", {A: {"scopes": ["read"]}}),
    "both": key("both", {A: {"scopes": FULL}, B: {"scopes": FULL}}),
    "alphaProject": A,
    "betaProject": B,
}
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump(keys, handle)
print(f"seeded: alpha={A} beta={B}; keys written to {OUT} (mode 0600, not printed)")
