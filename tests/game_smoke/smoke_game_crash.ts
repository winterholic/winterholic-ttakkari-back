import Phaser from 'phaser';
import { installBridge } from './bridge';

// 일부러 부팅 중 예외를 내는 게임: 일반 smoke 가 콘솔·페이지 에러와 빈 화면을 실패로 가려내는지 확인하는 용도다.
class CrashScene extends Phaser.Scene {
  constructor() {
    super('crash');
  }

  create(): void {
    throw new Error('boom: 의도한 부팅 오류');
  }
}

const game = new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [CrashScene],
  render: { preserveDrawingBuffer: true },
});
installBridge(() => game.canvas);
