// 렌더 전 카메라 동선 감사(headless Node). 통과하지 못하면 렌더하지 않는다.
// 검사: framing(주인공이 프레임 안) / occlusion(시선 가림) / clearance(카메라가 지형·거석을 뚫는지) /
//       continuity(프레임 간 위치·각도 점프) / floor(지형 아래로 내려가지 않음)
// 사용: node cinematic/audit.mjs [v1|v2] [--json]
import * as THREE from 'three';
import { PARAMS, FPS, WIDTH, HEIGHT, TOTAL_FRAMES, buildWorld, makeShots, cameraAt, heightAt, HERO_POS } from './world.js';

const ver = process.argv[2] && !process.argv[2].startsWith('--') ? process.argv[2] : 'v2';
const asJson = process.argv.includes('--json');
const P = PARAMS[ver];
const world = buildWorld(P, { headless: true });
const shots = makeShots(P);
const cam = new THREE.PerspectiveCamera(35, WIDTH / HEIGHT, 0.1, 900);
const ray = new THREE.Raycaster();
const near = 0.1;

// OBB 까지 거리(카메라 점 → 회전된 박스 표면)
function distToBox(p, b) {
  const inv = new THREE.Matrix4().copy(b.mesh.matrixWorld).invert();
  const lp = p.clone().applyMatrix4(inv);
  const dx = Math.max(Math.abs(lp.x) - b.half.x, 0), dy = Math.max(Math.abs(lp.y) - b.half.y, 0), dz = Math.max(Math.abs(lp.z) - b.half.z, 0);
  return Math.hypot(dx, dy, dz);
}

const report = [];
let prev = null;
for (const s of shots) {
  const r = { shot: s.id, frames: [s.start, s.end], checks: {} };
  let inFrame = 0, visible = 0, n = 0, minClear = Infinity, minAbove = Infinity, maxJump = 0, maxRot = 0, framingMargin = Infinity;
  const fails = [];
  for (let f = s.start; f < s.end; f++) {
    const cs = cameraAt(shots, f);
    cam.position.copy(cs.pos); cam.up.set(0, 1, 0); cam.lookAt(cs.target); cam.rotateZ(cs.roll);
    cam.fov = cs.fov; cam.updateProjectionMatrix(); cam.updateMatrixWorld(true);
    world.update(f);
    n++;
    // framing: 주인공 중심을 NDC 로 투영
    const hp = world.hero.position.clone();
    const ndc = hp.clone().project(cam);
    const inside = Math.abs(ndc.x) < 0.92 && Math.abs(ndc.y) < 0.85 && ndc.z < 1;
    if (inside) inFrame++;
    framingMargin = Math.min(framingMargin, 0.92 - Math.abs(ndc.x), 0.85 - Math.abs(ndc.y));
    // occlusion: 카메라 → 주인공 레이캐스트(거석·제단) + 지형 샘플
    const dir = hp.clone().sub(cs.pos); const dist = dir.length(); dir.normalize();
    ray.set(cs.pos, dir); ray.far = dist - 0.5;
    const hits = ray.intersectObjects(world.solids, false);
    let blockedTerrain = false;
    for (let k = 1; k <= 40; k++) { const q = cs.pos.clone().addScaledVector(dir, (dist * k) / 41); if (q.y < heightAt(q.x, q.z)) { blockedTerrain = true; break; } }
    if (hits.length === 0 && !blockedTerrain) visible++;
    // clearance
    for (const b of world.boxes) minClear = Math.min(minClear, distToBox(cs.pos, b));
    const above = cs.pos.y - heightAt(cs.pos.x, cs.pos.z);
    minAbove = Math.min(minAbove, above);
    // continuity (샷 안에서만)
    if (prev && f > s.start) {
      maxJump = Math.max(maxJump, cs.pos.distanceTo(prev.pos));
      maxRot = Math.max(maxRot, Math.abs(cs.fov - prev.fov));
    }
    prev = cs;
  }
  prev = null;
  const fracIn = inFrame / n, fracVis = visible / n;
  r.checks.framing = { inFrameRatio: +fracIn.toFixed(3), minNdcMargin: +framingMargin.toFixed(3), pass: fracIn >= 0.97 };
  r.checks.occlusion = { visibleRatio: +fracVis.toFixed(3), budget: 1 - s.occlusionBudget, pass: fracVis >= 1 - s.occlusionBudget };
  r.checks.clearance = { minDistToPillar: +minClear.toFixed(2), pass: minClear > near * 8 };
  r.checks.floor = { minHeightAboveGround: +minAbove.toFixed(2), pass: minAbove > 0.9 };
  r.checks.continuity = { maxPosJumpPerFrame: +maxJump.toFixed(3), maxFovStep: +maxRot.toFixed(3), pass: maxJump < 1.2 && maxRot < 0.5 };
  r.pass = Object.values(r.checks).every((c) => c.pass);
  report.push(r);
}
const ok = report.every((r) => r.pass);
if (asJson) console.log(JSON.stringify({ ver, ok, report }, null, 2));
else {
  console.log(`audit ${ver}: ${ok ? 'PASS' : 'FAIL'}`);
  for (const r of report) {
    console.log(` ${r.pass ? '✓' : '✗'} ${r.shot} [${r.frames}]`);
    for (const [k, c] of Object.entries(r.checks)) console.log(`     ${c.pass ? 'ok  ' : 'FAIL'} ${k} ${JSON.stringify(c)}`);
  }
}
process.exit(ok ? 0 : 1);
