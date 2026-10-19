import Phaser from 'phaser';
import { installBridge, publishState } from './bridge';
import { phaserPickAt } from './pick';

class MainScene extends Phaser.Scene {
  constructor() {
    super('main');
  }

  create(): void {
    const { width, height } = this.scale;
    this.add.text(width / 2, height / 2, 'Ttakkari Phaser Game', { color: '#ffffff', fontSize: '28px' }).setOrigin(0.5);
    publishState({ scene: 'main', ready: true }); // 부모 관제탑·자동화가 읽는 계측 값(점수·위치 등을 여기에 게시한다)
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

installBridge(() => game.canvas, {
  pause: () => game.pause(),
  resume: () => game.resume(),
  isPaused: () => game.isPaused,
  pickAt: (x, y) => phaserPickAt(game, x, y), // 관제탑의 오브젝트 선택. 오브젝트에 setName() 을 붙이면 선택 결과의 name 으로 보인다
});
