import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';

// 일부러 입력에 반응하지 않는 게임: 정적 화면, 고정된 계측 상태. 일반 smoke 가 이를 실패로 가려내는지 확인하는 용도다.
class StaticScene extends Phaser.Scene {
  constructor() {
    super('static');
  }

  create(): void {
    const { width, height } = this.scale;
    this.add.rectangle(width / 2, height / 2, width * 0.6, height * 0.3, 0x3366cc);
    this.add.text(width / 2, height / 2, 'static', { color: '#ffffff', fontSize: '28px' }).setOrigin(0.5);
    publishState({ ready: true });
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [StaticScene],
  render: { preserveDrawingBuffer: true },
});
installBridge(() => game.canvas, { pause: () => game.pause(), resume: () => game.resume(), isPaused: () => game.isPaused });
