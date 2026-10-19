import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';
import { createTextures, DOOR_H, DOOR_W, TEX } from './sprites';

// 논리 해상도는 세로 모바일 기준(480x640)이고 Scale.FIT 이 390x844, 800x600 등 어느 화면에서도 통째로 맞춘다.
const W = 480;
const H = 640;
const HUD_H = 56;
const ROOM_TOP = HUD_H;
const ROOM_BOTTOM = 536;
const WALL = 16;
const SPRITE_SCALE = 3;

const BODY_HALF = 12; // 이동 제한용 반폭
const CONTACT_DIST = 20; // 플레이어-적 접촉 거리(두 반경 합)
const FLOOR_L = WALL + BODY_HALF;
const FLOOR_R = W - WALL - BODY_HALF;
const FLOOR_T = ROOM_TOP + WALL + BODY_HALF;
const FLOOR_B = ROOM_BOTTOM - WALL - BODY_HALF;

const MAX_HP = 3;
const ROOMS = 3;
const PLAYER_SPEED = 150;
const ENEMY_SPEED = [66, 76, 88]; // 방별 속도. 모두 플레이어 최고 속도의 60%(90) 이하
const ENEMY_COUNT = [2, 3, 4];
const ENEMY_HP = 2;
const ATTACK_RADIUS = 56;
const ATTACK_COOLDOWN = 300;
const INVULN_MS = 800;
const ATTACK_FX_MS = 160;

const DOOR_X = W - WALL / 2;
const DOOR_Y = (ROOM_TOP + ROOM_BOTTOM) / 2;
const DOOR_TOUCH_MARGIN = 6;

const BTN = { x: 410, y: 588, r: 46 };

const FONT = '"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif';

type Mode = 'play' | 'gameover' | 'victory';

interface Enemy {
  sprite: Phaser.GameObjects.Image;
  hp: number;
  flashUntil: number;
}

class MainScene extends Phaser.Scene {
  private mode: Mode = 'play';
  private room = 1;
  private hp = MAX_HP;
  private kills = 0;
  private doorOpen = false;
  private lastAttack = -10000;
  private invulnUntil = 0;
  private attackFxStart = -10000;

  private player!: Phaser.GameObjects.Image;
  private door!: Phaser.GameObjects.Image;
  private enemies: Enemy[] = [];
  private hearts: Phaser.GameObjects.Image[] = [];
  private roomText!: Phaser.GameObjects.Text;
  private killText!: Phaser.GameObjects.Text;
  private hintText!: Phaser.GameObjects.Text;
  private fx!: Phaser.GameObjects.Graphics;
  private button!: Phaser.GameObjects.Graphics;
  private overlay!: Phaser.GameObjects.Container;
  private overlayTitle!: Phaser.GameObjects.Text;
  private overlaySub!: Phaser.GameObjects.Text;

  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private wasd!: Record<'W' | 'A' | 'S' | 'D', Phaser.Input.Keyboard.Key>;
  private spaceKey!: Phaser.Input.Keyboard.Key;
  private restartKey!: Phaser.Input.Keyboard.Key;

  private movePointer: Phaser.Input.Pointer | null = null;
  private attackPointer: Phaser.Input.Pointer | null = null;
  private attackQueued = false;

  constructor() {
    super('main');
  }

  create(): void {
    createTextures(this);
    this.buildRoomVisuals();
    this.buildHud();
    this.buildOverlay();
    this.setupInput();
    this.startRun();
    this.publish(0);
  }

  // ---------- 화면 구성 ----------

  private buildRoomVisuals(): void {
    this.add.tileSprite(0, ROOM_TOP, W, ROOM_BOTTOM - ROOM_TOP, TEX.floor).setOrigin(0, 0).setDepth(0);

    const walls = this.add.graphics().setDepth(1);
    const wallRects: Array<[number, number, number, number]> = [
      [0, ROOM_TOP, W, WALL],
      [0, ROOM_BOTTOM - WALL, W, WALL],
      [0, ROOM_TOP, WALL, ROOM_BOTTOM - ROOM_TOP],
      [W - WALL, ROOM_TOP, WALL, ROOM_BOTTOM - ROOM_TOP],
    ];
    for (const [x, y, w, h] of wallRects) {
      walls.fillStyle(0x5a4a3a, 1).fillRect(x, y, w, h);
      walls.fillStyle(0x3d3226, 1);
      if (w > h) for (let bx = 0; bx < w; bx += 16) walls.fillRect(x + bx, y + (bx % 32 === 0 ? 0 : 8), 2, 8);
      else for (let by = 0; by < h; by += 16) walls.fillRect(x + (by % 32 === 0 ? 0 : 8), y + by, 8, 2);
    }

    this.door = this.add.image(DOOR_X, DOOR_Y, TEX.doorClosed).setDepth(2);
    this.player = this.add.image(0, 0, TEX.player).setScale(SPRITE_SCALE).setDepth(6);
    this.fx = this.add.graphics().setDepth(7);

    // 하단 조작 영역
    this.add.rectangle(0, ROOM_BOTTOM, W, H - ROOM_BOTTOM, 0x14171f).setOrigin(0, 0).setDepth(0);
    this.button = this.add.graphics().setDepth(10);
    this.add
      .text(BTN.x, BTN.y, '공격', { fontFamily: FONT, fontSize: '24px', fontStyle: 'bold', color: '#ffffff' })
      .setOrigin(0.5)
      .setDepth(11);
    this.hintText = this.add
      .text(16, 588, '누르고 있는 방향으로 이동\n키보드: 방향키/WASD + 스페이스', {
        fontFamily: FONT,
        fontSize: '13px',
        color: '#8b93a7',
        lineSpacing: 6,
      })
      .setOrigin(0, 0.5)
      .setDepth(11);
  }

  private buildHud(): void {
    this.add.rectangle(0, 0, W, HUD_H, 0x0d0f14).setOrigin(0, 0).setDepth(9);
    for (let i = 0; i < MAX_HP; i++) {
      this.hearts.push(
        this.add
          .image(12 + i * 30, HUD_H / 2, TEX.heartFull)
          .setOrigin(0, 0.5)
          .setScale(SPRITE_SCALE)
          .setDepth(10),
      );
    }
    const style = { fontFamily: 'monospace', fontSize: '18px', fontStyle: 'bold', color: '#ffffff' };
    this.roomText = this.add.text(W / 2 + 20, HUD_H / 2, '', style).setOrigin(0.5).setDepth(10);
    this.killText = this.add.text(W - 12, HUD_H / 2, '', { ...style, color: '#ffd166' }).setOrigin(1, 0.5).setDepth(10);
  }

  private buildOverlay(): void {
    const dim = this.add.rectangle(0, 0, W, H, 0x000000, 0.72).setOrigin(0, 0);
    this.overlayTitle = this.add
      .text(W / 2, H / 2 - 40, '', { fontFamily: 'monospace', fontSize: '52px', fontStyle: 'bold', color: '#ff5252' })
      .setOrigin(0.5);
    this.overlaySub = this.add
      .text(W / 2, H / 2 + 30, '', { fontFamily: FONT, fontSize: '18px', color: '#ffffff', align: 'center', lineSpacing: 8 })
      .setOrigin(0.5);
    this.overlay = this.add.container(0, 0, [dim, this.overlayTitle, this.overlaySub]).setDepth(20).setVisible(false);
  }

  private setupInput(): void {
    const kb = this.input.keyboard;
    if (kb === null) throw new Error('keyboard input unavailable');
    this.cursors = kb.createCursorKeys();
    this.wasd = {
      W: kb.addKey(Phaser.Input.Keyboard.KeyCodes.W),
      A: kb.addKey(Phaser.Input.Keyboard.KeyCodes.A),
      S: kb.addKey(Phaser.Input.Keyboard.KeyCodes.S),
      D: kb.addKey(Phaser.Input.Keyboard.KeyCodes.D),
    };
    this.spaceKey = kb.addKey(Phaser.Input.Keyboard.KeyCodes.SPACE);
    this.restartKey = kb.addKey(Phaser.Input.Keyboard.KeyCodes.R);

    this.input.addPointer(2); // 이동 손가락 + 공격 버튼 손가락 동시 입력

    this.input.on('pointerdown', (p: Phaser.Input.Pointer) => {
      if (this.mode !== 'play') {
        this.startRun();
        return;
      }
      if (Phaser.Math.Distance.Between(p.x, p.y, BTN.x, BTN.y) <= BTN.r) {
        this.attackPointer = p;
        this.attackQueued = true;
        return;
      }
      if (this.movePointer === null || !this.movePointer.isDown) this.movePointer = p;
    });
    this.input.on('pointerup', (p: Phaser.Input.Pointer) => {
      if (p === this.movePointer) this.movePointer = null;
      if (p === this.attackPointer) this.attackPointer = null;
    });
  }

  // ---------- 진행 ----------

  private startRun(): void {
    this.mode = 'play';
    this.hp = MAX_HP;
    this.kills = 0;
    this.lastAttack = -10000;
    this.invulnUntil = 0;
    this.attackFxStart = -10000;
    this.movePointer = null;
    this.attackPointer = null;
    this.attackQueued = false;
    this.overlay.setVisible(false);
    this.loadRoom(1);
  }

  private loadRoom(room: number): void {
    this.room = room;
    this.doorOpen = false;
    this.door.setTexture(TEX.doorClosed);
    this.player.setPosition(FLOOR_L + 16, DOOR_Y).setAlpha(1).setFlipX(false);
    for (const e of this.enemies) e.sprite.destroy();
    this.enemies = [];

    const count = ENEMY_COUNT[room - 1];
    const spots: Array<{ x: number; y: number }> = [];
    for (let i = 0; i < count; i++) {
      let spot = { x: 0, y: 0 };
      for (let tries = 0; tries < 40; tries++) {
        spot = {
          x: Phaser.Math.Between(W / 2, FLOOR_R),
          y: Phaser.Math.Between(FLOOR_T, FLOOR_B),
        };
        if (spots.every((s) => Phaser.Math.Distance.Between(s.x, s.y, spot.x, spot.y) >= 56)) break;
      }
      spots.push(spot);
      const sprite = this.add.image(spot.x, spot.y, TEX.enemy).setScale(SPRITE_SCALE).setDepth(5);
      this.enemies.push({ sprite, hp: ENEMY_HP, flashUntil: 0 });
    }
    this.refreshHud();
  }

  private endRun(mode: 'gameover' | 'victory'): void {
    this.mode = mode;
    this.movePointer = null;
    this.attackPointer = null;
    this.attackQueued = false;
    if (mode === 'gameover') {
      this.overlayTitle.setText('GAME OVER').setColor('#ff5252');
      this.overlaySub.setText('다시 시작: R 키 또는 화면을 탭하세요');
    } else {
      this.overlayTitle.setText('VICTORY!').setColor('#ffd166');
      this.overlaySub.setText(`모든 방을 정복했다 (처치 ${this.kills})\n다시 시작: R 키 또는 화면을 탭하세요`);
    }
    this.overlay.setVisible(true);
  }

  private refreshHud(): void {
    this.hearts.forEach((h, i) => h.setTexture(i < this.hp ? TEX.heartFull : TEX.heartEmpty));
    this.roomText.setText(`ROOM ${this.room}/${ROOMS}`);
    this.killText.setText(`처치 ${this.kills}`);
  }

  // ---------- 매 프레임 ----------

  update(time: number, delta: number): void {
    const dt = Math.min(delta, 50) / 1000;

    if (Phaser.Input.Keyboard.JustDown(this.restartKey) && this.mode !== 'play') this.startRun();

    if (this.mode === 'play') {
      this.updatePlayer(dt);
      this.updateAttack(time);
      this.updateEnemies(dt, time);
      this.updateDoor();
    }

    this.drawEffects(time);
    this.publish(time);
  }

  private updatePlayer(dt: number): void {
    let dx = 0;
    let dy = 0;
    if (this.cursors.left.isDown || this.wasd.A.isDown) dx -= 1;
    if (this.cursors.right.isDown || this.wasd.D.isDown) dx += 1;
    if (this.cursors.up.isDown || this.wasd.W.isDown) dy -= 1;
    if (this.cursors.down.isDown || this.wasd.S.isDown) dy += 1;

    if (this.movePointer !== null && !this.movePointer.isDown) this.movePointer = null;
    if (this.movePointer !== null) {
      const tx = this.movePointer.x - this.player.x;
      const ty = this.movePointer.y - this.player.y;
      if (Math.hypot(tx, ty) > 6) {
        dx = tx;
        dy = ty;
      }
    }

    const len = Math.hypot(dx, dy);
    if (len > 0) {
      this.player.x += (dx / len) * PLAYER_SPEED * dt;
      this.player.y += (dy / len) * PLAYER_SPEED * dt;
      if (Math.abs(dx) > 0.01) this.player.setFlipX(dx < 0);
    }
    this.player.x = Phaser.Math.Clamp(this.player.x, FLOOR_L, FLOOR_R);
    this.player.y = Phaser.Math.Clamp(this.player.y, FLOOR_T, FLOOR_B);
  }

  private attackReady(time: number): boolean {
    return this.mode === 'play' && time - this.lastAttack >= ATTACK_COOLDOWN;
  }

  private updateAttack(time: number): void {
    const wants =
      this.attackQueued ||
      Phaser.Input.Keyboard.JustDown(this.spaceKey) ||
      this.spaceKey.isDown ||
      (this.attackPointer !== null && this.attackPointer.isDown);
    this.attackQueued = false;
    if (!wants || !this.attackReady(time)) return;

    this.lastAttack = time;
    this.attackFxStart = time;
    const px = this.player.x;
    const py = this.player.y;
    for (const e of [...this.enemies]) {
      const d = Phaser.Math.Distance.Between(px, py, e.sprite.x, e.sprite.y);
      if (d > ATTACK_RADIUS) continue;
      e.hp -= 1;
      if (e.hp <= 0) {
        e.sprite.destroy();
        this.enemies.splice(this.enemies.indexOf(e), 1);
        this.kills += 1;
        continue;
      }
      e.flashUntil = time + 120;
      const nx = d > 0 ? (e.sprite.x - px) / d : 1;
      const ny = d > 0 ? (e.sprite.y - py) / d : 0;
      e.sprite.x = Phaser.Math.Clamp(e.sprite.x + nx * 14, FLOOR_L, FLOOR_R);
      e.sprite.y = Phaser.Math.Clamp(e.sprite.y + ny * 14, FLOOR_T, FLOOR_B);
    }

    if (this.enemies.length === 0) {
      if (this.room >= ROOMS) {
        this.refreshHud();
        this.endRun('victory');
        return;
      }
      this.doorOpen = true;
      this.door.setTexture(TEX.doorOpen);
    }
    this.refreshHud();
  }

  private updateEnemies(dt: number, time: number): void {
    const speed = ENEMY_SPEED[this.room - 1];
    for (const e of this.enemies) {
      const s = e.sprite;
      const dx = this.player.x - s.x;
      const dy = this.player.y - s.y;
      const d = Math.hypot(dx, dy);
      if (d > 1) {
        s.x += (dx / d) * speed * dt;
        s.y += (dy / d) * speed * dt;
      }
      if (time < e.flashUntil) s.setTintFill(0xffffff);
      else s.clearTint();
    }

    // 적끼리 겹쳐 한 점으로 뭉치지 않게 살짝 밀어낸다
    for (let i = 0; i < this.enemies.length; i++) {
      for (let j = i + 1; j < this.enemies.length; j++) {
        const a = this.enemies[i].sprite;
        const b = this.enemies[j].sprite;
        const d = Phaser.Math.Distance.Between(a.x, a.y, b.x, b.y);
        if (d >= CONTACT_DIST || d === 0) continue;
        const push = (CONTACT_DIST - d) / 2;
        const nx = (a.x - b.x) / d;
        const ny = (a.y - b.y) / d;
        a.x += nx * push;
        a.y += ny * push;
        b.x -= nx * push;
        b.y -= ny * push;
      }
    }
    for (const e of this.enemies) {
      e.sprite.x = Phaser.Math.Clamp(e.sprite.x, FLOOR_L, FLOOR_R);
      e.sprite.y = Phaser.Math.Clamp(e.sprite.y, FLOOR_T, FLOOR_B);
    }

    if (time < this.invulnUntil) return;
    const touched = this.enemies.some((e) => Phaser.Math.Distance.Between(this.player.x, this.player.y, e.sprite.x, e.sprite.y) < CONTACT_DIST);
    if (!touched) return;
    this.hp = Math.max(0, this.hp - 1);
    this.invulnUntil = time + INVULN_MS;
    this.cameras.main.shake(120, 0.01);
    this.refreshHud();
    if (this.hp <= 0) this.endRun('gameover');
  }

  private updateDoor(): void {
    if (!this.doorOpen || this.room >= ROOMS) return;
    const nearX = Math.abs(this.player.x - DOOR_X) <= BODY_HALF + DOOR_W / 2 + DOOR_TOUCH_MARGIN;
    const nearY = Math.abs(this.player.y - DOOR_Y) <= BODY_HALF + DOOR_H / 2;
    if (nearX && nearY) this.loadRoom(this.room + 1);
  }

  // ---------- 표시/게시 ----------

  private drawEffects(time: number): void {
    // 무적 중 깜빡임
    const blink = this.mode === 'play' && time < this.invulnUntil && Math.floor(time / 80) % 2 === 0;
    this.player.setAlpha(blink ? 0.35 : 1);

    this.fx.clear();
    const age = time - this.attackFxStart;
    if (age >= 0 && age < ATTACK_FX_MS) {
      const t = age / ATTACK_FX_MS;
      this.fx.lineStyle(4, 0xffffff, 1 - t * 0.7);
      this.fx.strokeCircle(this.player.x, this.player.y, ATTACK_RADIUS * (0.4 + 0.6 * t));
    }

    const ready = this.attackReady(time);
    this.button.clear();
    this.button.fillStyle(ready ? 0xd94848 : 0x5a2b2b, 1).fillCircle(BTN.x, BTN.y, BTN.r);
    this.button.lineStyle(4, ready ? 0xffb4b4 : 0x7a4a4a, 1).strokeCircle(BTN.x, BTN.y, BTN.r);
  }

  private publish(time: number): void {
    publishState({
      scene: this.mode,
      room: this.room,
      hp: this.hp,
      maxHp: MAX_HP,
      kills: this.kills,
      doorOpen: this.doorOpen,
      player: { x: Math.round(this.player.x), y: Math.round(this.player.y) },
      enemies: this.mode === 'play' ? this.enemies.map((e) => ({ x: Math.round(e.sprite.x), y: Math.round(e.sprite.y), hp: e.hp })) : [],
      w: W,
      h: H,
      door: { x: DOOR_X, y: DOOR_Y },
      attackReady: this.attackReady(time),
    });
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#0d0f14',
  width: W,
  height: H,
  pixelArt: true,
  scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
  scene: [MainScene],
  render: { preserveDrawingBuffer: true }, // 부모의 캡처 요청이 WebGL 에서도 빈 이미지가 되지 않게 한다
});

installBridge(() => game.canvas, {
  pause: () => game.pause(),
  resume: () => game.resume(),
  isPaused: () => game.isPaused,
});
