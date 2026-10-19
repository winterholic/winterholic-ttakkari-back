// G27 오브젝트 픽킹 검증용 최소 게임(실제 Phaser 3 + 템플릿 bridge·pick). 겹친 오브젝트, 텍스처 이미지, 컨테이너 자식,
// 텍스트, 숨긴 오브젝트를 정해진 자리에 두고 publishState 로 월드 좌표를 게시한다. 시나리오는 이 좌표를 화면 비율로 바꿔 pick 을 요청한다.
import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';
import { phaserPickAt } from './pick';

class PickScene extends Phaser.Scene {
  constructor() {
    super('pickdemo');
  }

  create(): void {
    const { width, height } = this.scale;
    const g = this.make.graphics({ x: 0, y: 0 }, false);
    g.fillStyle(0xff5533, 1);
    g.fillRect(0, 0, 32, 32);
    g.generateTexture('crate-tex', 32, 32);
    g.destroy();

    this.add.rectangle(width * 0.25, height * 0.5, 80, 80, 0x225577).setName('shadow').setDepth(0);
    this.add.rectangle(width * 0.25, height * 0.5, 40, 40, 0x39d0a0).setName('player').setDepth(5);
    this.add.circle(width * 0.75, height * 0.5, 16, 0xffd54a).setName('coin');
    this.add.text(8, 8, 'score 0', { color: '#ffffff', fontSize: '18px' }).setName('score-label');
    this.add.image(width * 0.5, height * 0.8, 'crate-tex').setName('crate');
    this.add.rectangle(width * 0.5, height * 0.3, 60, 60, 0xffffff).setName('ghost').setVisible(false);
    const hud = this.add.container(width - 60, 40).setName('hud');
    hud.add(this.add.rectangle(0, 0, 24, 24, 0xe0457b).setName('heart'));

    this.at = {
      player: [width * 0.25, height * 0.5], shadowEdge: [width * 0.25 + 35, height * 0.5], coin: [width * 0.75, height * 0.5], label: [30, 17],
      crate: [width * 0.5, height * 0.8], ghost: [width * 0.5, height * 0.3], heart: [width - 60, 40], empty: [width * 0.06, height * 0.95],
    };
  }

  update(): void {
    this.frames += 1;
    publishState({ ready: true, frames: this.frames, w: this.scale.width, h: this.scale.height, at: this.at });
  }

  private at: Record<string, [number, number]> = {};
  private frames = 0;
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [PickScene],
  render: { preserveDrawingBuffer: true },
});

installBridge(() => game.canvas, {
  pause: () => game.pause(),
  resume: () => game.resume(),
  isPaused: () => game.isPaused,
  pickAt: (x, y) => phaserPickAt(game, x, y),
});
