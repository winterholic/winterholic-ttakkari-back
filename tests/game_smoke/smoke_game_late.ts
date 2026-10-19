// G16/G19 회귀용 게임: 첫 프레임(장면)이 2.5초 늦게 그려진다. bridge 는 먼저 응답하므로 부팅 직후 캡처는 단색 배경이다.
// 실제 worker 가 만든 게임에서 일반 smoke 가 이 순간의 캡처를 "빈 화면"으로 오판해 간헐 실패했다(smoke_generic.mjs BLANK_SETTLE_MS).
// 장면이 그려진 뒤의 동작은 smoke_game_main.ts 와 같다.
import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';

const SPEED = 240;

class PlayScene extends Phaser.Scene {
  private player!: Phaser.GameObjects.Rectangle;
  private coin!: Phaser.GameObjects.Arc;
  private cursors!: Phaser.Types.Input.Keyboard.CursorKeys;
  private target: { x: number; y: number } | null = null;
  private score = 0;
  private frames = 0;
  private label!: Phaser.GameObjects.Text;

  constructor() {
    super('play');
  }

  create(): void {
    const { width, height } = this.scale;
    this.player = this.add.rectangle(width * 0.25, height * 0.5, 28, 28, 0x39d0a0);
    this.coin = this.add.circle(width * 0.75, height * 0.5, 12, 0xffd54a);
    this.label = this.add.text(8, 8, 'score 0', { color: '#ffffff', fontSize: '18px' });
    this.cursors = this.input.keyboard!.createCursorKeys();
    // 터치만 이동 목표가 된다(데스크톱에서 화면을 눌러 포커스를 주는 클릭이 이동으로 섞이지 않게)
    this.input.on('pointerdown', (p: Phaser.Input.Pointer) => { if (p.wasTouch) this.target = { x: p.x, y: p.y }; });
    this.input.on('pointermove', (p: Phaser.Input.Pointer) => { if (p.isDown && p.wasTouch) this.target = { x: p.x, y: p.y }; });
  }

  update(_t: number, dt: number): void {
    const step = (SPEED * dt) / 1000;
    const k = this.cursors;
    if (k.left.isDown) this.player.x -= step;
    if (k.right.isDown) this.player.x += step;
    if (k.up.isDown) this.player.y -= step;
    if (k.down.isDown) this.player.y += step;
    if (this.target) { // 탭한 지점으로 이동(손을 떼도 도착할 때까지 유지)
      const dx = this.target.x - this.player.x;
      const dy = this.target.y - this.player.y;
      const d = Math.hypot(dx, dy);
      if (d <= 2) this.target = null;
      else {
        this.player.x += (dx / d) * Math.min(step, d);
        this.player.y += (dy / d) * Math.min(step, d);
      }
    }
    if (Phaser.Math.Distance.Between(this.player.x, this.player.y, this.coin.x, this.coin.y) < 24) {
      this.score += 1;
      const { width, height } = this.scale;
      this.coin.setPosition(Phaser.Math.Between(40, width - 40), Phaser.Math.Between(60, height - 40));
      this.label.setText('score ' + this.score);
    }
    this.frames += 1;
    publishState({
      scene: 'play', ready: true, x: Math.round(this.player.x), y: Math.round(this.player.y), score: this.score, frames: this.frames,
      coin: { x: Math.round(this.coin.x), y: Math.round(this.coin.y) }, w: this.scale.width, h: this.scale.height,
    });
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [],
  render: { preserveDrawingBuffer: true },
});

setTimeout(() => game.scene.add('play', PlayScene, true), 2500);

installBridge(() => game.canvas, { pause: () => game.pause(), resume: () => game.resume(), isPaused: () => game.isPaused });
