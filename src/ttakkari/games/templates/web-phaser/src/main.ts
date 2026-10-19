import Phaser from 'phaser';
import { installBridge } from './bridge';

class MainScene extends Phaser.Scene {
  constructor() {
    super('main');
  }

  create(): void {
    const { width, height } = this.scale;
    this.add.text(width / 2, height / 2, 'Ttakkari Phaser Game', { color: '#ffffff', fontSize: '28px' }).setOrigin(0.5);
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [MainScene],
  render: { preserveDrawingBuffer: true }, // 부모의 캡처 요청이 WebGL 에서도 빈 이미지가 되지 않게 한다
});

installBridge(() => game.canvas);
