import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';
import { phaserPickAt } from './pick';

const PAD = 50;
const CELL = 100;
const ROW = 120;

const cell = (col: number, row: number): { x: number; y: number } => ({ x: PAD + col * CELL, y: 80 + row * ROW });

class LevelScene extends Phaser.Scene {
  constructor() {
    super('level');
  }

  create(): void {
    const star = this.make.graphics({ x: 0, y: 0 }, false);
    star.fillStyle(0xfff176, 1);
    star.fillRect(0, 0, 28, 28);
    star.generateTexture('star-tex', 28, 28);
    star.destroy();

    const player = cell(1, 0);
    this.add.rectangle(player.x, player.y, 36, 36, 0x39d0a0).setName('player');
    const pickup = cell(2, 1);
    this.add.image(pickup.x, pickup.y, 'star-tex');
    const bannerPos = cell(3, 0);
    this.add.text(bannerPos.x, bannerPos.y, 'LEVEL 1', { color: '#ffffff', fontSize: '20px' }).setOrigin(0.5);
    const gateA = cell(1, 2);
    this.add.rectangle(gateA.x, gateA.y, 44, 28, 0x7e57c2);
    const gateB = cell(4, 2);
    this.add.rectangle(gateB.x, gateB.y, 44, 28, 0x7e57c2);
    const treeA = cell(0, 4);
    this.add.circle(treeA.x, treeA.y, 20, 0x388e3c).setName('tree');
    const treeB = cell(5, 4);
    this.add.circle(treeB.x, treeB.y, 20, 0x388e3c).setName('tree');
  }

  update(): void {
    publishState({ ready: true, w: this.scale.width, h: this.scale.height });
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [LevelScene],
  render: { preserveDrawingBuffer: true },
});

installBridge(() => game.canvas, {
  pause: () => game.pause(),
  resume: () => game.resume(),
  isPaused: () => game.isPaused,
  pickAt: (x, y) => phaserPickAt(game, x, y),
});
