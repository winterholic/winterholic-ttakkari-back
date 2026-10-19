// G14 시나리오: 2D 로그라이크의 규칙을 실제 브라우저에서 조작해 확인한다
// (이동·공격·적 처치·체력 감소·게임오버·재시작·방 이동·터치). 상태는 bridge 의 ttakkari:state(게임이 publishState 로 게시)로만 읽는다.
// 결과는 @@RESULT@@ JSON 한 줄. cfg: playwright_dir, play_url, sandbox, host_port, out_dir, tag(파일 이름 접두), mobile(선택)
import path from 'node:path';
import { GameSession, loadPlaywright, emit } from './ttk_driver.mjs';

const cfg = JSON.parse(process.argv[2]);
const pw = loadPlaywright(cfg.playwright_dir);
const tag = cfg.tag ?? 'rl';
const out = (n) => path.join(cfg.out_dir, tag + '-' + n + '.png');
const R = { checks: {}, notes: [], shots: {} };

// 키를 잠깐 눌렀다 뗀다. 프레임 사이에 down/up 이 한꺼번에 처리되면 isDown 을 보는 게임이 입력을 놓치므로 90ms 유지한다.
async function tapKey(s, key) { await s.page.keyboard.down(key); await s.page.waitForTimeout(90); await s.page.keyboard.up(key); }

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

async function hold(s, keys) {
  for (const k of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) {
    if (keys.includes(k)) await s.page.keyboard.down(k); else await s.page.keyboard.up(k);
  }
}

async function release(s) { await hold(s, []); }

/** 목표 지점을 향해 방향키로 이동한다. 도착(stop 거리 이내)하거나 시간이 다 되면 멈춘다. */
async function steer(s, target, { stop = 14, timeoutMs = 8000 } = {}) {
  const end = Date.now() + timeoutMs;
  let st = await s.state();
  while (Date.now() < end && st.scene === 'play') {
    const t = typeof target === 'function' ? target(st) : target;
    if (!t) break;
    const dx = t.x - st.player.x;
    const dy = t.y - st.player.y;
    if (Math.hypot(dx, dy) <= stop) break;
    await hold(s, [dx < -6 ? 'ArrowLeft' : null, dx > 6 ? 'ArrowRight' : null, dy < -6 ? 'ArrowUp' : null, dy > 6 ? 'ArrowDown' : null].filter(Boolean));
    await s.page.waitForTimeout(40);
    st = await s.state();
  }
  await release(s);
  return st;
}

const nearest = (st) => [...st.enemies].sort((a, b) => dist(a, st.player) - dist(b, st.player))[0];

/** 현재 방의 적을 모두 처치한다. 게임오버가 되면 중단한다. */
async function clearRoom(s, { timeoutMs = 60000 } = {}) {
  const end = Date.now() + timeoutMs;
  let st = await s.state();
  while (Date.now() < end && st.scene === 'play' && st.enemies.length > 0) {
    const e = nearest(st);
    st = await steer(s, () => e && { x: e.x, y: e.y }, { stop: 30, timeoutMs: 1500 });
    for (let i = 0; i < 3 && st.scene === 'play'; i += 1) {
      await tapKey(s, 'Space');
      await s.page.waitForTimeout(60);
    }
    st = await s.state();
  }
  return st;
}

async function enterDoor(s, { timeoutMs = 15000 } = {}) {
  let st = await s.state();
  const end = Date.now() + timeoutMs;
  const room = st.room;
  while (Date.now() < end && st.scene === 'play' && st.room === room) {
    st = await steer(s, (c) => c.door, { stop: 10, timeoutMs: 2000 });
    st = await s.state();
    if (st.room === room) await s.page.waitForTimeout(150);
  }
  return st;
}

const s = await GameSession.open({
  pw, hostPort: cfg.host_port, hostOrigin: cfg.host_origin ?? null, playUrl: cfg.play_url, sandbox: cfg.sandbox, mobile: !!cfg.mobile,
  viewport: cfg.mobile ? { width: 390, height: 844 } : { width: 800, height: 600 },
});
try {
  const s0 = await s.waitState((st) => st && st.scene === 'play' && Array.isArray(st.enemies) && st.player, { label: 'boot', timeoutMs: 20000 });
  R.initial = s0;
  R.checks.state_shape = ['scene', 'room', 'hp', 'maxHp', 'kills', 'doorOpen', 'player', 'enemies', 'door'].every((k) => k in s0) && s0.hp === 3 && s0.room === 1 && s0.kills === 0;
  R.shots.initial = await s.capture(out('01-initial'));
  await s.pageShot(out('01-initial-page'));
  await s.focusGame();

  // 이동: 방향키로 플레이어 위치가 바뀐다.
  await s.page.keyboard.down('ArrowRight');
  const moved = await s.waitState((st) => st.scene !== 'play' || Math.abs(st.player.x - s0.player.x) > 20, { label: 'move', timeoutMs: 5000 });
  await s.page.keyboard.up('ArrowRight');
  R.checks.move = Math.abs(moved.player.x - s0.player.x) > 20;
  R.move = { from: s0.player, to: moved.player };

  if (cfg.mobile) {
    // 터치: 누르고 있는 방향으로 이동, 오른쪽 아래 '공격' 버튼 탭으로 공격 쿨다운 시작.
    const st1 = await s.state();
    const fx = Math.min(0.9, (st1.player.x + 120) / st1.w);
    const fy = st1.player.y / st1.h;
    await s.touchDown(fx, fy);
    const tm = await s.waitState((st) => Math.abs(st.player.x - st1.player.x) > 15, { label: 'touch move', timeoutMs: 5000 });
    await s.touchUp();
    R.checks.touch_move = Math.abs(tm.player.x - st1.player.x) > 15;
    R.touch = { from: st1.player, to: tm.player };
    await s.page.waitForTimeout(400);
    // 공격 버튼은 FIT 로 줄어든 캔버스의 오른쪽 아래에 있다. 화면 비율에 따라 위치가 달라 후보 지점을 차례로 탭해 본다.
    let sawCooldown = false;
    for (const [bx, by] of [[0.85, 0.76], [0.85, 0.9], [0.88, 0.9], [0.85, 0.82]]) {
      await s.page.waitForTimeout(450);
      await s.tap(bx, by);
      for (let i = 0; i < 8 && !sawCooldown; i += 1) { if ((await s.state()).attackReady === false) sawCooldown = true; else await s.page.waitForTimeout(40); }
      if (sawCooldown) { R.touchAttackAt = [bx, by]; break; }
    }
    R.checks.touch_attack_button = sawCooldown;
    R.shots.mobile = await s.capture(out('02-mobile'));
    await s.pageShot(out('02-mobile-page'));
  }

  // 공격·처치·피격: 가장 가까운 적을 쫓아 스페이스로 친다.
  const before = await s.state();
  const e0 = before.enemies.length;
  let after = before;
  const t0 = Date.now();
  // clear_first: 방1 을 일부러 완전히 비워 간헐 실패 경로(처치 단계가 방을 비운 경우)를 결정적으로 재현한다.
  while (Date.now() - t0 < 40000 && after.scene === 'play' && (cfg.clear_first ? after.enemies.length > 0 : after.kills === before.kills)) {
    const e = nearest(after);
    after = await steer(s, () => e && { x: e.x, y: e.y }, { stop: 30, timeoutMs: 1500 });
    for (let i = 0; i < 3 && after.scene === 'play'; i += 1) {
      await s.page.keyboard.down('Space');
      await s.page.waitForTimeout(100);
      if (!R.shots.attackFx) R.shots.attackFx = await s.capture(out('02b-attack-fx')); // 공격 직후 이펙트가 보이는 순간
      await s.page.waitForTimeout(10);
      await s.page.keyboard.up('Space');
      await s.page.waitForTimeout(60);
    }
    after = await s.state();
  }
  R.checks.attack_kills_enemy = after.kills > before.kills || after.scene !== 'play';
  R.combat = { enemiesBefore: e0, killsBefore: before.kills, killsAfter: after.kills, hpAfter: after.hp };
  R.shots.afterKill = await s.capture(out('03-after-kill'));

  // 게임오버: 가만히 서서 적에게 맞는다. 체력이 줄다가 0 이 되면 gameover.
  // 방1 의 적은 2마리뿐이고 한 번 휘두르면 둘 다 반경 안에 들 수 있다(스폰 위치는 무작위, 적이 모이는 속도는 봇의 반응 속도에 따라 달라진다).
  // 처치 단계가 방을 비워 버리면 맞을 적이 없어 60초를 기다리다 실패했다(배포 게이트 808a5496 의 실패 원인). 그때는 열린 문으로 방2 에 들어가
  // 적이 있는 방에서 기다린다. 적은 항상 플레이어에게 직진하므로 이 경로는 시간에 따라 결과가 갈리지 않는다.
  if (after.scene === 'play' && after.enemies.length === 0) {
    R.notes.push('처치 단계에서 방1 이 비어 방2 로 이동해 게임오버를 기다린다');
    R.roomClearedInKillPhase = true;
    if (after.doorOpen) after = await enterDoor(s, { timeoutMs: 45000 });
    R.checks.moved_on_after_clear = after.scene === 'play' && after.room === 2 && after.enemies.length > 0;
  }
  let hpSeen = [after.hp];
  const go = await (async () => {
    const end = Date.now() + 60000;
    let st = await s.state();
    while (Date.now() < end && st.scene === 'play') {
      await s.page.waitForTimeout(250);
      st = await s.state();
      if (hpSeen[hpSeen.length - 1] !== st.hp) hpSeen.push(st.hp);
    }
    return st;
  })();
  R.hpSeen = hpSeen;
  R.checks.hp_decreases = hpSeen.length > 1 || go.hp < 3;
  R.checks.game_over = go.scene === 'gameover' && go.hp <= 0;
  R.gameover = { scene: go.scene, hp: go.hp, kills: go.kills };
  R.shots.gameover = await s.capture(out('04-gameover'));
  await s.pageShot(out('04-gameover-page'));

  // 재시작: R 키로 처음 상태(방1, 체력 3, 처치 0)로 돌아온다.
  // 최대 3번 시도하고 몇 번째에 성공했는지 남긴다(입력이 프레임 사이에서 유실되는 경우를 숨기지 않기 위해).
  let re = null;
  R.restartAttempts = 0;
  for (let i = 0; i < 3 && !re; i += 1) {
    R.restartAttempts += 1;
    if (cfg.mobile) await s.tap(0.5, 0.5); // 터치 환경에서는 화면 탭으로 재시작(요구 사항)
    else { await s.page.keyboard.down('r'); await s.page.waitForTimeout(120); await s.page.keyboard.up('r'); }
    re = await s.waitState((st) => st.scene === 'play', { label: 'restart', timeoutMs: 1500 }).catch(() => null);
  }
  R.checks.restart = !!re && re.room === 1 && re.hp === 3 && re.kills === 0;
  R.restart = re && { room: re.room, hp: re.hp, kills: re.kills, enemies: re.enemies.length };
  R.shots.restart = await s.capture(out('05-restart'));

  // quick 모드는 방 이동 검증을 건너뛴다(자동 테스트 시간 절약).
  if (!cfg.quick) {
    // 방 이동: 방1 을 정리하면 문이 열리고 문으로 가면 방2 로 간다.
    let st = await clearRoom(s);
    R.room1 = { scene: st.scene, hp: st.hp, enemies: st.enemies.length, doorOpen: st.doorOpen };
    if (st.scene === 'play' && st.enemies.length === 0) {
      R.checks.door_opens_after_clear = st.doorOpen === true;
      st = await enterDoor(s);
      R.checks.room_transition = st.room === 2;
      R.shots.room2 = await s.capture(out('06-room2'));
      await s.pageShot(out('06-room2-page'));
      // 방2, 방3 도 이어서 시도(시간 제한). 성공하면 승리 화면까지 간다.
      for (const room of [2, 3]) {
        if (st.scene !== 'play' || st.room !== room) break;
        st = await clearRoom(s);
        if (st.scene !== 'play' || st.enemies.length > 0) break;
        if (room === 2) st = await enterDoor(s);
        else await s.page.waitForTimeout(300), (st = await s.state());
      }
      R.final = { scene: st.scene, room: st.room, hp: st.hp, kills: st.kills };
      R.checks.victory_or_progress = st.scene === 'victory' || st.room >= 2;
    } else {
      R.notes.push('방1 정리 중 게임오버 또는 시간 초과: ' + JSON.stringify(R.room1));
      R.checks.door_opens_after_clear = false;
      R.checks.room_transition = false;
    }
    R.shots.final = await s.capture(out('07-final'));
    await s.pageShot(out('07-final-page'));
  }
} catch (e) {
  R.error = String(e.stack ?? e);
}
R.console = s.collected;
await s.close();
emit(R);
