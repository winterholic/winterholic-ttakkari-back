// 도달성 검사: 레벨 명세(JSON)만으로 '스폰이 끼었나·상호작용/문/출구에 닿을 수 있나'를 격자 탐색으로 확인한다.
// 게임별 하드코딩이 없다. 게임은 solids·spawns·targets 를 명세로 내보내기만 하면 된다(references/level-spec.md).
// 원형: ceopark 의 tools/check_mansion_reach.mjs (Mansion 내부 필드에 박혀 있어 다른 게임에 못 썼다). 같은 BFS 를 명세 입력으로 일반화했다.
import { readJson } from './util.mjs';

function rectsHit(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}

function distPointRect(px, py, r) {
  const dx = Math.max(r.x - px, 0, px - (r.x + r.w));
  const dy = Math.max(r.y - py, 0, py - (r.y + r.h));
  return Math.hypot(dx, dy);
}

export function checkRoom(room, defaults = {}) {
  const player = { w: 20, h: 20, ox: -10, oy: -10, ...(defaults.player ?? {}), ...(room.player ?? {}) };
  const step = room.step ?? defaults.step ?? 8;
  const bounds = room.bounds ?? defaults.bounds;
  if (!bounds) throw new Error(`방 ${room.id}: bounds{w,h} 가 필요하다`);
  const solids = room.solids ?? [];
  const blocked = (x, y) => {
    const r = { x: x + player.ox, y: y + player.oy, w: player.w, h: player.h };
    if (r.x < 0 || r.y < 0 || r.x + r.w > bounds.w || r.y + r.h > bounds.h) return true;
    return solids.some((s) => rectsHit(s, r));
  };
  const report = { room: room.id, spawns: [], problems: [] };
  for (const sp of room.spawns ?? []) {
    const entry = { spawn: sp.id, from: sp.from ?? null, x: sp.x, y: sp.y, stuck: blocked(sp.x, sp.y), cells: 0, unreachable: [] };
    if (entry.stuck) {
      report.problems.push(`스폰 ${sp.id}(${sp.x},${sp.y})가 충돌체 안·경계 밖이다: 진입하면 움직일 수 없다`);
      report.spawns.push(entry);
      continue;
    }
    const sx = Math.round(sp.x / step) * step;
    const sy = Math.round(sp.y / step) * step;
    const start = blocked(sx, sy) ? [sp.x, sp.y] : [sx, sy];
    const key = (x, y) => `${x},${y}`;
    const seen = new Set([key(...start)]);
    const stack = [start];
    while (stack.length) {
      const [x, y] = stack.pop();
      for (const [dx, dy] of [[step, 0], [-step, 0], [0, step], [0, -step]]) {
        const nx = x + dx;
        const ny = y + dy;
        const k = key(nx, ny);
        if (seen.has(k) || blocked(nx, ny)) continue;
        seen.add(k);
        stack.push([nx, ny]);
      }
    }
    const cells = [...seen].map((k) => k.split(',').map(Number));
    entry.cells = cells.length;
    for (const t of room.targets ?? []) {
      if (t.required === false) continue;
      const reach = t.reach ?? 40;
      const ok = cells.some(([cx, cy]) => (t.rect ? distPointRect(cx, cy, t.rect) : Math.hypot(cx - t.x, cy - t.y)) <= reach);
      if (!ok) {
        entry.unreachable.push(`${t.kind ?? 'target'}:${t.id}`);
        report.problems.push(`스폰 ${sp.id}에서 ${t.kind ?? 'target'} ${t.id} 에 닿을 수 없다(reach ${reach})`);
      }
    }
    const minCells = room.minCells ?? defaults.minCells ?? 40;
    if (entry.cells < minCells) report.problems.push(`스폰 ${sp.id}에서 갈 수 있는 곳이 ${entry.cells}칸뿐이다: 작은 구덩이에 갇혔을 수 있다(최소 ${minCells})`);
    report.spawns.push(entry);
  }
  if (!(room.spawns ?? []).length) report.problems.push('스폰이 하나도 없다: 도달성을 검사할 수 없다');
  return report;
}

export function checkReach(specFile) {
  const spec = readJson(specFile);
  const rooms = spec.rooms ?? [{ id: spec.name ?? 'room', ...spec }];
  const reports = rooms.map((r) => checkRoom(r, spec));
  const problems = reports.flatMap((r) => r.problems.map((p) => `[${r.room}] ${p}`));
  return {
    tool: 'studio-reach',
    ok: problems.length === 0,
    spec: specFile,
    step: spec.step ?? 8,
    rooms: reports,
    problems,
    limits: ['격자(step) 해상도의 근사다. 좁은 통로는 step 을 줄여 다시 확인한다.', '명세가 실제 게임의 충돌체를 그대로 내보냈는지는 이 도구가 증명하지 못한다(명세 추출기와 실플레이가 따로 필요).', '움직이는 장애물·점프·대시는 모델링하지 않는다.'],
  };
}
