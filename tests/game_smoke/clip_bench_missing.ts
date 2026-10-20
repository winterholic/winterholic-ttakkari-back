// 빨강·초록·파랑 공 세 개가 나란히 화면을 왼쪽에서 오른쪽으로 가로지른다. 위치와 현재 보이는 공을 매 프레임 게시한다.
import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';

const TRAVERSE_MS = 10000;

class PlayScene extends Phaser.Scene {
  private red!: Phaser.GameObjects.Arc;
  private green!: Phaser.GameObjects.Arc;
  private blue!: Phaser.GameObjects.Arc;
  private born = 0;
  private frames = 0;

  constructor() {
    super('play');
  }

  create(): void {
    const { width, height } = this.scale;
    this.red = this.add.circle(width * 0.08, height * 0.35, 22, 0xff4444);
    this.green = this.add.circle(width * 0.08, height * 0.5, 22, 0x33dd66);
    this.blue = this.add.circle(width * 0.08, height * 0.65, 22, 0x3366ff);
    this.born = this.time.now;
  }

  update(): void {
    const { width, height } = this.scale;
    const p = Math.min((this.time.now - this.born) / TRAVERSE_MS, 1);
    const bx = 0.08 + 0.84 * p;
    this.red.setPosition(bx * width, height * 0.35);
    this.green.setPosition(bx * width, height * 0.5);
    this.blue.setPosition(bx * width, height * 0.65);
    this.green.setVisible(bx <= 0.5);
    this.frames += 1;
    publishState({ ready: true, frames: this.frames, bx: +bx.toFixed(3), green: bx <= 0.5, w: width, h: height });
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
