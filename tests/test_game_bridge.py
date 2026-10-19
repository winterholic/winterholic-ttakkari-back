"""게임 템플릿 bridge 의 부모 origin·source·스키마 검증 (docs/adr/0049 postMessage 계약). 순수 함수를 node 로 직접 실행한다."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BRIDGE = Path(__file__).parent.parent / "src/ttakkari/games/templates/web-phaser/src/bridge.ts"
requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")

SCRIPT = """
import { acceptParentMessage, parentOriginFromHash } from %s;
const parent = {}; const other = {};
const ok = { origin: 'https://pwa.example', source: parent, data: { type: 'ttakkari:capture', id: 'abc' } };
const cases = {
  ok: acceptParentMessage(ok, parent, 'https://pwa.example'),
  wrong_origin: acceptParentMessage({ ...ok, origin: 'https://evil.example' }, parent, 'https://pwa.example'),
  wrong_source: acceptParentMessage({ ...ok, source: other }, parent, 'https://pwa.example'),
  no_parent_hint: acceptParentMessage(ok, parent, null),
  bad_type: acceptParentMessage({ ...ok, data: { type: 'eval', id: 'abc' } }, parent, 'https://pwa.example'),
  no_id: acceptParentMessage({ ...ok, data: { type: 'ttakkari:capture' } }, parent, 'https://pwa.example'),
  long_id: acceptParentMessage({ ...ok, data: { type: 'ttakkari:capture', id: 'x'.repeat(65) } }, parent, 'https://pwa.example'),
  array: acceptParentMessage({ ...ok, data: [] }, parent, 'https://pwa.example'),
  string: acceptParentMessage({ ...ok, data: 'ttakkari:capture' }, parent, 'https://pwa.example'),
  null_data: acceptParentMessage({ ...ok, data: null }, parent, 'https://pwa.example'),
  pause: acceptParentMessage({ ...ok, data: { type: 'ttakkari:pause', id: 'p1' } }, parent, 'https://pwa.example'),
  resume: acceptParentMessage({ ...ok, data: { type: 'ttakkari:resume', id: 'p2' } }, parent, 'https://pwa.example'),
  state: acceptParentMessage({ ...ok, data: { type: 'ttakkari:state', id: 's1' } }, parent, 'https://pwa.example'),
  pause_wrong_origin: acceptParentMessage({ origin: 'https://evil.example', source: parent, data: { type: 'ttakkari:pause', id: 'p1' } }, parent, 'https://pwa.example'),
  pause_wrong_source: acceptParentMessage({ origin: 'https://pwa.example', source: other, data: { type: 'ttakkari:pause', id: 'p1' } }, parent, 'https://pwa.example'),
  import_ok: acceptParentMessage({ ...ok, data: { type: 'ttakkari:save-import', id: 'i1', data: '{"a":1}' } }, parent, 'https://pwa.example'),
  import_no_data: acceptParentMessage({ ...ok, data: { type: 'ttakkari:save-import', id: 'i1' } }, parent, 'https://pwa.example'),
  import_huge: acceptParentMessage({ ...ok, data: { type: 'ttakkari:save-import', id: 'i1', data: 'x'.repeat(65 * 1024) } }, parent, 'https://pwa.example'),
  hash_ok: parentOriginFromHash('#ttk-parent=' + encodeURIComponent('https://pwa.example')),
  hash_port: parentOriginFromHash('#ttk-parent=' + encodeURIComponent('http://localhost:5173')),
  hash_path: parentOriginFromHash('#ttk-parent=' + encodeURIComponent('https://pwa.example/x')),
  hash_js: parentOriginFromHash('#ttk-parent=' + encodeURIComponent('javascript:alert(1)')),
  hash_none: parentOriginFromHash(''),
};
console.log(JSON.stringify(cases));
"""


@requires_node
def test_bridge_accepts_only_the_declared_parent_and_known_messages():
    out = subprocess.run(["node", "--input-type=module", "-e", SCRIPT % json.dumps(BRIDGE.as_uri())], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr[-800:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["ok"] == {"type": "ttakkari:capture", "id": "abc"}
    for rejected in ("wrong_origin", "wrong_source", "no_parent_hint", "bad_type", "no_id", "long_id", "array", "string", "null_data"):
        assert got[rejected] is None, rejected
    assert got["pause"] == {"type": "ttakkari:pause", "id": "p1"} and got["resume"] == {"type": "ttakkari:resume", "id": "p2"}
    assert got["state"] == {"type": "ttakkari:state", "id": "s1"}
    assert got["import_ok"] == {"type": "ttakkari:save-import", "id": "i1", "data": '{"a":1}'}
    for rejected in ("pause_wrong_origin", "pause_wrong_source", "import_no_data", "import_huge"):
        assert got[rejected] is None, rejected
    assert got["hash_ok"] == "https://pwa.example" and got["hash_port"] == "http://localhost:5173"
    assert got["hash_path"] is None and got["hash_js"] is None and got["hash_none"] is None


def test_bridge_never_posts_to_a_wildcard_origin():
    text = BRIDGE.read_text()
    assert "postMessage" in text and "'*'" not in text and '"*"' not in text


READY_SCRIPT = """
import { installBridge, save } from %s;
const sent = [];
const listeners = [];
const parent = { postMessage: (msg, origin) => sent.push({ msg, origin }) };
globalThis.window = { location: { hash: '#ttk-parent=' + encodeURIComponent('https://pwa.example') }, parent,
  addEventListener: (type, fn) => { if (type === 'message') listeners.push(fn); } };
installBridge(() => null, {});
const readyMessages = sent.filter((s) => s.msg.type === 'ttakkari:ready');
const state = (p) => Promise.race([p.then(() => 'resolved'), new Promise((r) => setTimeout(() => r('pending'), 30))]);
const before = await state(save.loaded);
// 부모가 아닌 창이 보낸 import 는 save.loaded 를 풀지 못한다
listeners[0]({ origin: 'https://evil.example', source: parent, data: { type: 'ttakkari:save-import', id: 'i0', data: '{"a":1}' } });
const afterForged = await state(save.loaded);
const forgedValue = save.get('a');
listeners[0]({ origin: 'https://pwa.example', source: parent, data: { type: 'ttakkari:save-import', id: 'i1', data: '{"level":4}' } });
const after = await state(save.loaded);
const replies = sent.filter((s) => s.msg.type === 'ttakkari:save-import:result');
console.log(JSON.stringify({ readyMessages, before, afterForged, forgedValue: forgedValue ?? null, after, level: save.get('level'), replies }));
process.exit(0);
"""


@requires_node
def test_bridge_announces_ready_and_resolves_save_loaded_only_from_the_parent():
    out = subprocess.run(["node", "--input-type=module", "-e", READY_SCRIPT % json.dumps(BRIDGE.as_uri())], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr[-800:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["readyMessages"] == [{"msg": {"type": "ttakkari:ready"}, "origin": "https://pwa.example"}]  # 부모 origin 으로만 알린다
    assert got["before"] == "pending" and got["afterForged"] == "pending" and got["forgedValue"] is None
    assert got["after"] == "resolved" and got["level"] == 4
    assert got["replies"] == [{"msg": {"type": "ttakkari:save-import:result", "ok": True, "id": "i1"}, "origin": "https://pwa.example"}]
