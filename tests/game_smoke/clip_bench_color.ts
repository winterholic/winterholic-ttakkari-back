// 빨간 공이 화면을 왼쪽에서 오른쪽으로 가로지른다. 공의 위치와 색을 매 프레임 게시한다.
import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';

const TRAVERSE_MS = 10000;

class PlayScene extends Phaser.Scene {
  private ball!: Phaser.GameObjects.Arc;
  private born = 0;
  private frames = 0;

  constructor() {
    super('play');
  }

  create(): void {
    const { width, height } = this.scale;
    this.ball = this.add.circle(width * 0.08, height * 0.5, 30, 0xff4444);
    this.born = this.time.now;
  }

  update(): void {
    const { width, height } = this.scale;
    const p = Math.min((this.time.now - this.born) / TRAVERSE_MS, 1);
    const bx = 0.08 + 0.84 * p;
    const tint = bx > 0.6 ? 0x3366ff : 0xff4444;
    this.ball.setPosition(bx * width, height * 0.5);
    this.ball.setFillStyle(tint);
    this.frames += 1;
    publishState({ ready: true, frames: this.frames, bx: +bx.toFixed(3), color: tint === 0xff4444 ? 'red' : 'blue', w: width, h: height });
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [PlayScene],
  render: { preserveDrawingBuffer: true },
});

installBridge(() => game.canvas, { pause: () => game.pause(), resume: () => game.resume(), isPaused: () => game.isPaused });
