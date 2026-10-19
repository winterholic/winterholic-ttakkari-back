import Phaser from 'phaser';

class MainScene extends Phaser.Scene {
  constructor() {
    super('main');
  }

  create(): void {
    const { width, height } = this.scale;
    this.add.text(width / 2, height / 2, 'Ttakkari Phaser Game', { color: '#ffffff', fontSize: '28px' }).setOrigin(0.5);
  }
}

new Phaser.Game({
  type: Phaser.AUTO,
  parent: 'game',
  backgroundColor: '#1d2330',
  scale: { mode: Phaser.Scale.RESIZE, width: '100%', height: '100%' },
  scene: [MainScene],
});
