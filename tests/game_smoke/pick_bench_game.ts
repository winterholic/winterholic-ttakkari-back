// G29b 소스 매핑 측정용 게임: 이름이 있는 오브젝트, 이름 없이 텍스처 키만 있는 것, 이름 없는 텍스트, 이름도 텍스처도 없는 비슷한 도형,
// 같은 이름이 둘인 오브젝트가 섞여 있다. 측정 도구(pick_bench.py)가 각 오브젝트의 소스 줄을 좌표 문자열로 찾으므로 이 줄들의 좌표 표현을 바꾸지 않는다.
import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';
import { phaserPickAt } from './pick';

class BenchScene extends Phaser.Scene {
  constructor() {
    super('bench');
  }

  create(): void {
    const { width, height } = this.scale;
    for (const [key, color] of [['coin-tex', 0xffd54a], ['gem-tex', 0x55d0ff]] as const) {
      const g = this.make.graphics({ x: 0, y: 0 }, false);
      g.fillStyle(color, 1);
      g.fillRect(0, 0, 32, 32);
      g.generateTexture(key, 32, 32);
      g.destroy();
    }
    this.add.rectangle(width * 0.2, height * 0.15, 40, 40, 0x39d0a0).setName('hero');
    this.add.circle(width * 0.8, height * 0.15, 20, 0xe0457b).setName('enemy');
    this.add.text(width * 0.5, height * 0.05, 'score 0', { color: '#ffffff', fontSize: '18px' }).setOrigin(0.5).setName('hud-score');
    this.add.image(width * 0.2, height * 0.35, 'coin-tex');
    this.add.image(width * 0.8, height * 0.35, 'gem-tex');
    this.add.text(width * 0.5, height * 0.5, 'GET READY', { color: '#ffffff', fontSize: '22px' }).setOrigin(0.5);
    this.add.rectangle(width * 0.2, height * 0.65, 50, 30, 0x8855ff);
    this.add.rectangle(width * 0.8, height * 0.65, 50, 30, 0xff8855);
    this.add.circle(width * 0.25, height * 0.85, 18, 0x33aa33).setName('tree');
    this.add.circle(width * 0.75, height * 0.85, 18, 0x33aa33).setName('tree');
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
  scene: [BenchScene],
  render: { preserveDrawingBuffer: true },
});

installBridge(() => game.canvas, {
  pause: () => game.pause(),
  resume: () => game.resume(),
  isPaused: () => game.isPaused,
  pickAt: (x, y) => phaserPickAt(game, x, y),
});
