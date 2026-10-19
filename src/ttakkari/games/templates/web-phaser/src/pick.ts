// Phaser 오브젝트 픽킹(ADR 0059): 부모의 pick 요청(화면 비율 좌표)에 그 지점의 게임 오브젝트 관측 정보를 돌려준다.
// 이름·종류·텍스처 키·화면 경계처럼 Phaser 가 알려 주는 값만이다. 소스 위치(source_hint)는 알 수 없으므로 담지 않고,
// 소스 후보 조사는 이 정보를 받은 worker 몫이다. 게임이 setName() 으로 붙인 이름이 있을수록 소스에서 찾기 쉽다.
import type Phaser from 'phaser';
import type { PickBounds, PickHit, PickResult } from './bridge';

const MAX_HITS = 10;
const MAX_TEXT_CHARS = 60;
const MAX_NAME_CHARS = 80;
const MAX_NESTING = 4;

interface Visit {
  sceneIndex: number;
  sortDepth: number;
  order: number;
  hit: PickHit;
}

const round = (value: number): number => Math.round(value * 10000) / 10000;
const clip = (value: unknown, max: number): string => String(value ?? '').slice(0, max);

function textureKey(obj: Phaser.GameObjects.GameObject): string | null {
  if (obj.type === 'Text') return null; // Text 의 텍스처는 글자를 그리려고 Phaser 가 만든 내부 캔버스라 키(UUID)가 소스와 무관하다
  const texture = (obj as { texture?: { key?: unknown } }).texture;
  return texture && typeof texture.key === 'string' ? clip(texture.key, MAX_NAME_CHARS) : null;
}

function frameName(obj: Phaser.GameObjects.GameObject): string | null {
  const frame = (obj as { frame?: { name?: unknown } }).frame;
  return frame && (typeof frame.name === 'string' || typeof frame.name === 'number') && String(frame.name) !== '__BASE' ? clip(frame.name, MAX_NAME_CHARS) : null;
}

function plainText(obj: Phaser.GameObjects.GameObject): string | null {
  const text = (obj as { text?: unknown }).text;
  return typeof text === 'string' ? clip(text, MAX_TEXT_CHARS) : null;
}

/** x, y 는 캔버스의 0~1 비율(왼쪽 위가 0,0)이다. 활성 씬마다 보이는 오브젝트 중 그 지점을 덮는 것을 위에 그려진 순서로 돌려준다. */
export function phaserPickAt(game: Phaser.Game, x: number, y: number): PickResult {
  const width = game.scale.width;
  const height = game.scale.height;
  const sx = x * width;
  const sy = y * height;
  const visited: Visit[] = [];
  const scenes = game.scene.getScenes(true);
  let order = 0;

  scenes.forEach((scene, sceneIndex) => {
    const cam = scene.cameras.main;
    const world = cam.getWorldPoint(sx, sy);
    const record = (obj: Phaser.GameObjects.GameObject, container: string | null, isFixed: boolean, sortDepth: number): void => {
      const display = obj as unknown as { depth?: number };
      if (typeof (obj as { getBounds?: unknown }).getBounds !== 'function') return;
      const b = (obj as unknown as { getBounds(): Phaser.Geom.Rectangle }).getBounds();
      if (b.width <= 0 || b.height <= 0) return;
      const px = isFixed ? sx : world.x;
      const py = isFixed ? sy : world.y;
      if (px < b.left || px > b.right || py < b.top || py > b.bottom) return;
      const left = isFixed ? b.x : (b.x - cam.worldView.x) * cam.zoom;
      const top = isFixed ? b.y : (b.y - cam.worldView.y) * cam.zoom;
      const bounds: PickBounds = {
        x: round(left / width), y: round(top / height),
        width: round((b.width * (isFixed ? 1 : cam.zoom)) / width), height: round((b.height * (isFixed ? 1 : cam.zoom)) / height),
      };
      order += 1;
      visited.push({
        sceneIndex, sortDepth, order,
        hit: { scene: scene.scene.key, name: clip(obj.name, MAX_NAME_CHARS), type: obj.type, texture: textureKey(obj), frame: frameName(obj), text: plainText(obj),
          container, depth: display.depth ?? 0, bounds },
      });
    };
    const walk = (obj: Phaser.GameObjects.GameObject, container: string | null, fixed: boolean, nesting: number, parentDepth: number | null): void => {
      const display = obj as unknown as { visible?: boolean; alpha?: number; scrollFactorX?: number; scrollFactorY?: number; depth?: number };
      if (display.visible === false || display.alpha === 0) return; // 안 보이는 오브젝트는 자식까지 건너뛴다
      const isFixed = fixed || (display.scrollFactorX === 0 && display.scrollFactorY === 0); // 카메라를 따라 움직이지 않는 HUD 는 화면 좌표로 본다
      const sortDepth = parentDepth ?? display.depth ?? 0; // 컨테이너 자식의 depth 는 컨테이너 안에서만 뜻이 있어, 화면에서는 컨테이너 깊이를 따른다
      record(obj, container, isFixed, sortDepth);
      const list = (obj as { list?: Phaser.GameObjects.GameObject[] }).list;
      if (Array.isArray(list) && nesting < MAX_NESTING) {
        const name = clip(obj.name, MAX_NAME_CHARS);
        for (const child of list) walk(child, name || container || obj.type, isFixed, nesting + 1, sortDepth);
      }
    };
    for (const obj of scene.children.list) walk(obj, null, false, 0, null);
  });

  // 위에 그려진 것부터: 나중 씬 > 깊이(depth) > 나중에 방문한 것. 컨테이너는 자식보다 먼저 기록되므로 같은 깊이에서 자식이 앞에 온다.
  visited.sort((a, b) => b.sceneIndex - a.sceneIndex || b.sortDepth - a.sortDepth || b.order - a.order);
  return { scenes: scenes.map((s) => s.scene.key), hits: visited.slice(0, MAX_HITS).map((v) => v.hit), truncated: visited.length > MAX_HITS };
}
