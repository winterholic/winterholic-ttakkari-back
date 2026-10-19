import Phaser from 'phaser';

// 외부 이미지 없이 문자 격자로 픽셀 스프라이트 텍스처를 만든다. 각 문자는 palette 의 색 하나이고 '.' 은 투명이다.
type Palette = Record<string, string>;

function createCanvasTexture(scene: Phaser.Scene, key: string, w: number, h: number, draw: (ctx: CanvasRenderingContext2D) => void): void {
  if (scene.textures.exists(key)) return;
  const tex = scene.textures.createCanvas(key, w, h);
  if (tex === null) return;
  draw(tex.context);
  tex.refresh();
}

function pixelSprite(scene: Phaser.Scene, key: string, rows: string[], palette: Palette): void {
  createCanvasTexture(scene, key, rows[0].length, rows.length, (ctx) => {
    rows.forEach((row, y) => {
      for (let x = 0; x < row.length; x++) {
        const color = palette[row[x]];
        if (color === undefined) continue;
        ctx.fillStyle = color;
        ctx.fillRect(x, y, 1, 1);
      }
    });
  });
}

const PLAYER = [
  '..kkkk..',
  '.kssssk.',
  '.skksks.',
  '..ssss..',
  '.bbbbbb.',
  'sbbbbbbs',
  '.bb..bb.',
  '.ll..ll.',
];

const ENEMY = [
  '........',
  '..dddd..',
  '.gggggg.',
  'gwkggwkg',
  'gggggggg',
  'gggddggg',
  'gggggggg',
  '.dd..dd.',
];

const HEART = [
  '.rr..rr.',
  'rwrrrrrr',
  'rrrrrrrr',
  'rrrrrrrr',
  '.rrrrrr.',
  '..rrrr..',
  '...rr...',
  '........',
];

export const TEX = {
  player: 'px-player',
  enemy: 'px-enemy',
  heartFull: 'px-heart-full',
  heartEmpty: 'px-heart-empty',
  floor: 'px-floor',
  doorClosed: 'px-door-closed',
  doorOpen: 'px-door-open',
} as const;

export const DOOR_W = 16;
export const DOOR_H = 64;

export function createTextures(scene: Phaser.Scene): void {
  pixelSprite(scene, TEX.player, PLAYER, { k: '#4a3320', s: '#f2c9a0', b: '#3a7bd5', l: '#2a3f6b' });
  pixelSprite(scene, TEX.enemy, ENEMY, { g: '#4caf50', d: '#2e7d32', w: '#ffffff', k: '#111111' });
  pixelSprite(scene, TEX.heartFull, HEART, { r: '#e53935', w: '#ffcdd2' });
  pixelSprite(scene, TEX.heartEmpty, HEART, { r: '#3a3f4b', w: '#3a3f4b' });

  createCanvasTexture(scene, TEX.floor, 32, 32, (ctx) => {
    ctx.fillStyle = '#2b3345';
    ctx.fillRect(0, 0, 32, 32);
    ctx.fillStyle = '#313a4f';
    ctx.fillRect(0, 0, 16, 16);
    ctx.fillRect(16, 16, 16, 16);
    ctx.fillStyle = '#252c3b';
    ctx.fillRect(4, 22, 2, 2);
    ctx.fillRect(24, 6, 2, 2);
    ctx.fillRect(10, 8, 2, 2);
    ctx.fillRect(26, 26, 2, 2);
  });

  createCanvasTexture(scene, TEX.doorClosed, DOOR_W, DOOR_H, (ctx) => {
    ctx.fillStyle = '#3b2a1a';
    ctx.fillRect(0, 0, DOOR_W, DOOR_H);
    ctx.fillStyle = '#7a7f8a';
    for (let y = 4; y < DOOR_H; y += 12) ctx.fillRect(0, y, DOOR_W, 4);
    ctx.fillStyle = '#9aa0ab';
    for (let x = 2; x < DOOR_W; x += 6) ctx.fillRect(x, 0, 2, DOOR_H);
  });

  createCanvasTexture(scene, TEX.doorOpen, DOOR_W, DOOR_H, (ctx) => {
    ctx.fillStyle = '#05060a';
    ctx.fillRect(0, 0, DOOR_W, DOOR_H);
    ctx.fillStyle = '#f4d35e';
    ctx.fillRect(0, 0, DOOR_W, 4);
    ctx.fillRect(0, DOOR_H - 4, DOOR_W, 4);
    ctx.fillStyle = '#c9a227';
    ctx.fillRect(6, 24, 4, 16);
  });
}
