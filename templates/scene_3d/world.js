// 시네마틱 3D 장면 정의. 브라우저(렌더)와 Node(사전 감사)가 같은 파일을 쓴다.
// 원칙(threejs-previz에서 가져온 방법론, 코드는 새로 작성):
//  - 모든 애니메이션은 frame 의 순수 함수. Math.random/시계 금지, 시드 rng 만 사용.
//  - 샷 범위는 반열림 [start, end). 컷은 하드컷.
//  - 렌더 전에 카메라 동선을 Node 에서 측정(감사)한다. (audit.mjs)
import * as THREE from 'three';

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;
export const TOTAL_FRAMES = 420; // 14 s

// ---- 파라미터: v1(첫 렌더) / v2(검수 후 수정). 수정 이력은 research 문서에 기록. ----
export const PARAMS = {
  v1: {
    exposure: 1.0, fogDensity: 0.030, fogColor: 0x1b2433, hemi: 0.35, moon: 1.6, crystalLight: 120,
    bloom: [0.9, 0.6, 0.85], dofAperture: 0.00012, dofMaxBlur: 0.012, grain: 0.05, vignette: 0.55,
    particles: 2200, particleSize: 2.4, fogSprites: 70, fogSpriteOpacity: 0.10, letterbox: 0.0, ca: 0.0016,
    shotCams: { s1: {}, s2: {}, s3: {} },
  },
  v2: {
    exposure: 0.95, fogDensity: 0.015, fogColor: 0x1e2b45, hemi: 0.7, moon: 2.4, crystalLight: 110,
    crystalRough: 0.38, pulseGain: 0.55, emissiveBase: 0.75, emissivePulse: 0.8, haloBase: 8, haloPulse: 1.6,
    bloom: [0.5, 0.5, 0.86], dofAperture: 0.00007, dofMaxBlur: 0.007, grain: 0.035, vignette: 0.5,
    particles: 4200, particleSize: 3.2, burstSize: 7.5, fogSprites: 120, fogSpriteOpacity: 0.10, letterbox: 0.115, ca: 0.0012,
    beamBase: 0.08, beamPulse: 0.14, runes: true, rocks: 420,
    shotCams: { s1: { endR: 15 }, s2: { lowY: 1.9 }, s3: { r0: 17, r1: 9.5 } },
  },
};

// ---------- 결정적 난수·노이즈 ----------
export function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const hash2 = (x, y) => {
  let h = (x * 374761393 + y * 668265263) | 0;
  h = (h ^ (h >>> 13)) * 1274126177;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
};
const smooth = (t) => t * t * (3 - 2 * t);
function vnoise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y);
  const xf = x - xi, yf = y - yi;
  const a = hash2(xi, yi), b = hash2(xi + 1, yi), c = hash2(xi, yi + 1), d = hash2(xi + 1, yi + 1);
  const u = smooth(xf), v = smooth(yf);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}
function fbm(x, y, oct = 4) {
  let s = 0, amp = 0.5, f = 1;
  for (let i = 0; i < oct; i++) { s += amp * vnoise(x * f, y * f); f *= 2; amp *= 0.5; }
  return s;
}
const lerp = (a, b, t) => a + (b - a) * t;
const clamp01 = (t) => Math.min(1, Math.max(0, t));
const easeInOutSine = (u) => -(Math.cos(Math.PI * u) - 1) / 2;
const easeOutCubic = (u) => 1 - Math.pow(1 - u, 3);
const easeInOutCubic = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);

// ---------- 지형 높이(해석식). 메시와 감사가 같은 함수를 쓴다 ----------
export function heightAt(x, z) {
  const r = Math.hypot(x, z);
  let h = (fbm(x * 0.028 + 11.3, z * 0.028 - 4.7) - 0.45) * 15 + (fbm(x * 0.11, z * 0.11) - 0.5) * 1.6;
  const flat = smooth(clamp01((r - 7) / 16)); // 중앙 공터는 평평하게
  h *= flat;
  return h;
}

// ---------- 샷 정의 ----------
const V3 = (x, y, z) => new THREE.Vector3(x, y, z);
export const HERO_POS = V3(0, 5.2, 0);

function handheld(frame, amp = 0.05, rot = 0.0035, seed = 1) {
  const t = frame / FPS;
  const n = (k) => (vnoise(t * 1.7 + seed * 31.7 + k * 17.3, k * 3.1) - 0.5) * 2;
  return { dx: n(0) * amp, dy: n(1) * amp, dz: n(2) * amp * 0.5, rx: n(3) * rot, ry: n(4) * rot, rz: n(5) * rot * 0.6 };
}

export function makeShots(P) {
  const c = P.shotCams;
  const s1End = c.s1.endR ?? 17;
  const lowY = c.s2.lowY ?? 1.5;
  const r0 = c.s3.r0 ?? 18, r1 = c.s3.r1 ?? 8.5;
  return [
    {
      id: 's1_establishing_crane', start: 0, end: 150, intent: '안개 낀 거석 평원 전경에서 수정으로 내려오며 공간과 주인공을 소개',
      hero: HERO_POS, occlusionBudget: 0.15,
      pose(u, frame) {
        const e = easeInOutSine(u);
        const pos = V3(lerp(-40, -s1End * 0.82, e), lerp(23, 6.4, easeOutCubic(u)), lerp(52, s1End * 0.58, e));
        const target = V3(lerp(0, 0, e), lerp(5.5, 5.4, e), 0);
        const h = handheld(frame, 0.035, 0.0025, 1);
        pos.x += h.dx; pos.y += h.dy; pos.z += h.dz;
        return { pos, target, fov: lerp(30, 36, e), roll: lerp(0.0, -0.02, e) + h.rz, focus: pos.distanceTo(target), dofOn: 0.6 };
      },
    },
    {
      id: 's2_low_track_rackfocus', start: 150, end: 270, intent: '낮은 앵글 트래킹, 앞 거석에서 수정으로 초점 이동(랙포커스)',
      hero: HERO_POS, occlusionBudget: 0.45, // 전경 거석이 지나가는 것이 의도
      pose(u, frame) {
        const e = easeInOutSine(u);
        const ang = lerp(205, 150, e) * Math.PI / 180;
        const R = lerp(19, 14.5, e);
        const pos = V3(Math.sin(ang) * R, lowY + lerp(0, 0.5, e), Math.cos(ang) * R);
        pos.y = Math.max(pos.y, heightAt(pos.x, pos.z) + 1.2);
        const target = V3(0, lerp(4.2, 5.2, e), 0);
        const h = handheld(frame, 0.06, 0.004, 2);
        pos.x += h.dx; pos.y += h.dy; pos.z += h.dz;
        const dHero = pos.distanceTo(target);
        // 랙포커스: 전반은 전경(가까운 거리), 후반에 수정으로
        const fgDist = 7.5;
        const f = lerp(fgDist, dHero, easeInOutCubic(clamp01((u - 0.30) / 0.45)));
        return { pos, target, fov: 27, roll: 0.05 + h.rz, focus: f, dofOn: 1.0 };
      },
    },
    {
      id: 's3_orbit_pushin_pulse', start: 270, end: 420, intent: '수정 주위를 돌며 밀고 들어가 에너지 펄스(클라이맥스)와 페이드 아웃',
      hero: HERO_POS, occlusionBudget: 0.15,
      pose(u, frame) {
        const e = easeInOutSine(u);
        const ang = lerp(215, 335, e) * Math.PI / 180;
        const R = lerp(r0, r1, easeInOutCubic(u));
        const y = lerp(3.4, 5.8, easeOutCubic(u));
        const pos = V3(Math.sin(ang) * R, y, Math.cos(ang) * R);
        pos.y = Math.max(pos.y, heightAt(pos.x, pos.z) + 1.2);
        const target = V3(0, lerp(5.0, 5.3, e), 0);
        const h = handheld(frame, 0.03, 0.002, 3);
        pos.x += h.dx; pos.y += h.dy; pos.z += h.dz;
        return { pos, target, fov: lerp(40, 29, easeInOutCubic(u)), roll: lerp(0.03, -0.015, e) + h.rz, focus: pos.distanceTo(target), dofOn: 0.8 };
      },
    },
  ];
}

export function shotAt(shots, frame) {
  for (const s of shots) if (frame >= s.start && frame < s.end) return s;
  return shots[shots.length - 1];
}

export function cameraAt(shots, frame) {
  const s = shotAt(shots, frame);
  const u = clamp01((frame - s.start) / (s.end - s.start - 1));
  return { shot: s, u, ...s.pose(u, frame) };
}

// 에너지 펄스: s3 중반(frame 332)에서 터지고 감쇠. 수정 발광·조명·블룸·파티클 버스트가 공유
export const PULSE_FRAME = 332;
let PULSE_GAIN = 1.55; // buildWorld 가 P.pulseGain 으로 설정
export function pulseAt(frame) {
  const t = (frame - PULSE_FRAME) / FPS;
  const idle = 0.12 * Math.sin(frame / FPS * 2.0) + 0.10;
  if (t < 0) return idle + 0.25 * smooth(clamp01((t + 1.2) / 1.2)); // 직전 차오름
  return idle + 0.25 + (PULSE_GAIN) * Math.exp(-t * 1.6);
}

// ---------- 월드 구성 ----------
export function buildWorld(P, { headless = false } = {}) {
  P = { crystalRough: 0.15, pulseGain: 1.55, emissiveBase: 1.6, emissivePulse: 2.4, haloBase: 11, haloPulse: 7, beamBase: 0.07, beamPulse: 0.22, runes: false, rocks: 0, burstSize: 2.4, ...P };
  PULSE_GAIN = P.pulseGain;
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(P.fogColor, P.fogDensity);
  const solids = []; // 가림(occlusion)·충돌 측정 대상
  const boxes = []; // 해석적 충돌용 OBB 정보
  const rng = mulberry32(20261021);

  // 지형
  const SEG = headless ? 8 : 200;
  const tg = new THREE.PlaneGeometry(240, 240, SEG, SEG);
  tg.rotateX(-Math.PI / 2);
  const pos = tg.attributes.position;
  const col = new Float32Array(pos.count * 3);
  for (let i = 0; i < pos.count; i++) {
    const x = pos.getX(i), z = pos.getZ(i);
    const h = heightAt(x, z);
    pos.setY(i, h);
    const k = clamp01((h + 4) / 10);
    const tint = 0.55 + 0.45 * k + (vnoise(x * 0.4, z * 0.4) - 0.5) * 0.22 + (fbm(x * 0.09 + 40, z * 0.09) - 0.5) * 0.5;
    const warm = clamp01(vnoise(x * 0.05, z * 0.05 + 9) - 0.45) * 0.5;
    col[i * 3] = (0.16 + warm * 0.25) * tint; col[i * 3 + 1] = (0.20 + warm * 0.08) * tint; col[i * 3 + 2] = 0.27 * tint;
  }
  tg.setAttribute('color', new THREE.BufferAttribute(col, 3));
  tg.computeVertexNormals();
  const terrain = new THREE.Mesh(tg, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.96, metalness: 0.0 }));
  terrain.receiveShadow = true;
  terrain.name = 'terrain';
  scene.add(terrain);

  // 중앙 제단
  const ped = new THREE.Mesh(new THREE.CylinderGeometry(4.2, 5.0, 0.7, 48), new THREE.MeshStandardMaterial({ color: 0x2f3744, roughness: 0.8, metalness: 0.1 }));
  ped.position.set(0, 0.2, 0); ped.receiveShadow = true; ped.castShadow = true; ped.name = 'pedestal';
  scene.add(ped); solids.push(ped);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(3.4, 0.07, 12, 96), new THREE.MeshBasicMaterial({ color: 0x8be9ff }));
  ring.rotation.x = Math.PI / 2; ring.position.y = 0.56; ring.name = 'rune_ring';
  scene.add(ring);

  // 거석
  const pillars = [];
  const N = 18;
  for (let i = 0; i < N; i++) {
    const a = i * 2.399963 + (rng() - 0.5) * 0.35;
    const r = 11 + Math.sqrt(rng()) * 26;
    const w = 1.3 + rng() * 1.6, d = 1.0 + rng() * 1.3, hgt = 6 + rng() * 11;
    const g = new THREE.BoxGeometry(w, hgt, d, 2, 8, 2);
    const gp = g.attributes.position;
    for (let k = 0; k < gp.count; k++) {
      const jx = (hash2(k, i * 7) - 0.5) * 0.18, jz = (hash2(k + 91, i * 7 + 3) - 0.5) * 0.18;
      gp.setX(k, gp.getX(k) + jx * (Math.abs(gp.getY(k)) < hgt / 2 - 0.01 ? 1 : 0.2));
      gp.setZ(k, gp.getZ(k) + jz * (Math.abs(gp.getY(k)) < hgt / 2 - 0.01 ? 1 : 0.2));
    }
    g.computeVertexNormals();
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: 0x3a4351, roughness: 0.88, metalness: 0.04 }));
    const x = Math.cos(a) * r, z = Math.sin(a) * r;
    const base = heightAt(x, z);
    m.position.set(x, base + hgt / 2 - 1.2, z);
    m.rotation.set((rng() - 0.5) * 0.12, rng() * Math.PI, (rng() - 0.5) * 0.12);
    m.castShadow = true; m.receiveShadow = true; m.name = `pillar_${i}`;
    m.updateMatrixWorld(true);
    scene.add(m); solids.push(m); pillars.push(m);
    boxes.push({ mesh: m, half: V3(w / 2, hgt / 2, d / 2) });
  }

  // 거석 룬: 중앙을 향한 면에 발광 줄무늬. 에너지 펄스가 수정에서 바깥으로 퍼지는 것을 보여 준다(서사 장치)
  const runes = [];
  if (P.runes) {
    boxes.forEach((b, i) => {
      const m = b.mesh;
      const toC = V3(-m.position.x, 0, -m.position.z).normalize();
      const inv = new THREE.Quaternion().copy(m.quaternion).invert();
      const lc = toC.clone().applyQuaternion(inv);
      const alongX = Math.abs(lc.x) > Math.abs(lc.z);
      const sgn = alongX ? Math.sign(lc.x) : Math.sign(lc.z);
      const hx = b.half.x, hz = b.half.z, hy = b.half.y;
      const mat = new THREE.MeshBasicMaterial({ color: 0x66e0ff, toneMapped: true });
      const grp = new THREE.Group();
      const mk = (w, h, d, x, y, z) => { const r = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat); r.position.set(x, y, z); grp.add(r); };
      const off = alongX ? hx + 0.03 : hz + 0.03;
      const span = alongX ? hz : hx;
      for (const k of [-0.45, 0.45]) {
        if (alongX) mk(0.06, hy * 1.2, 0.07, sgn * off, -hy * 0.15, k * span * 0.9); else mk(0.07, hy * 1.2, 0.06, k * span * 0.9, -hy * 0.15, sgn * off);
      }
      for (let g = 0; g < 4; g++) {
        const y = hy * (0.15 + g * 0.17);
        if (alongX) mk(0.06, 0.07, span * 1.5, sgn * off, y, 0); else mk(span * 1.5, 0.07, 0.06, 0, y, sgn * off);
      }
      m.add(grp);
      runes.push({ mat, delay: Math.hypot(m.position.x, m.position.z) * 1.1 });
    });
  }

  // 바위 조각(지형 디테일)
  if (!headless && P.rocks > 0) {
    const rg = new THREE.IcosahedronGeometry(0.6, 0);
    const im = new THREE.InstancedMesh(rg, new THREE.MeshStandardMaterial({ color: 0x2c3442, roughness: 0.95, flatShading: true }), P.rocks);
    const dm = new THREE.Object3D();
    for (let i = 0; i < P.rocks; i++) {
      const a = rng() * Math.PI * 2, r = 6 + Math.pow(rng(), 0.7) * 70;
      const x = Math.cos(a) * r, z = Math.sin(a) * r, sc = 0.25 + Math.pow(rng(), 3) * 2.2;
      dm.position.set(x, heightAt(x, z) + sc * 0.15, z);
      dm.scale.set(sc * (0.8 + rng() * 0.6), sc * (0.5 + rng() * 0.5), sc * (0.8 + rng() * 0.6));
      dm.rotation.set(rng() * 3, rng() * 6, rng() * 3);
      dm.updateMatrix(); im.setMatrixAt(i, dm.matrix);
    }
    im.castShadow = true; im.receiveShadow = true; im.name = 'rocks';
    scene.add(im);
  }

  // 하늘 돔
  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(700, headless ? 8 : 48, headless ? 8 : 32),
    new THREE.ShaderMaterial({
      side: THREE.BackSide, depthWrite: false, fog: false,
      uniforms: { uMoonDir: { value: V3(-0.45, 0.42, -0.78).normalize() } },
      vertexShader: `varying vec3 vDir; void main(){ vDir = normalize(position); gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0);} `,
      fragmentShader: `
        varying vec3 vDir; uniform vec3 uMoonDir;
        float h21(vec2 p){ p=fract(p*vec2(123.34,456.21)); p+=dot(p,p+45.32); return fract(p.x*p.y);}
        void main(){
          float y = clamp(vDir.y,-0.2,1.0);
          vec3 horizon = vec3(0.30,0.24,0.30);
          vec3 mid = vec3(0.07,0.10,0.20);
          vec3 zen = vec3(0.012,0.02,0.06);
          vec3 c = mix(horizon, mid, smoothstep(0.0,0.22,y));
          c = mix(c, zen, smoothstep(0.18,0.85,y));
          vec2 sp = vec2(atan(vDir.z,vDir.x)*60.0, vDir.y*60.0);
          float st = step(0.9965, h21(floor(sp)));
          c += st * smoothstep(0.1,0.5,y) * vec3(0.8,0.85,1.0) * (0.4+0.6*h21(floor(sp)+3.1));
          float md = dot(normalize(vDir), uMoonDir);
          c += vec3(0.65,0.78,1.0) * smoothstep(0.9993,0.9998,md) * 1.6;
          c += vec3(0.25,0.35,0.6) * pow(max(md,0.0), 90.0) * 0.8;
          gl_FragColor = vec4(c,1.0);
        }`,
    })
  );
  sky.name = 'sky'; sky.renderOrder = -10;
  scene.add(sky);

  // 조명
  const hemi = new THREE.HemisphereLight(0x7e96c9, 0x1a1f2b, P.hemi);
  scene.add(hemi);
  const moon = new THREE.DirectionalLight(0x9bb7ff, P.moon);
  moon.position.set(-0.45 * 120, 0.42 * 120 + 20, -0.78 * 120);
  moon.castShadow = !headless;
  moon.shadow.mapSize.set(2048, 2048);
  const sc = moon.shadow.camera; sc.left = -60; sc.right = 60; sc.top = 60; sc.bottom = -60; sc.near = 1; sc.far = 320;
  moon.shadow.bias = -0.0004; moon.shadow.normalBias = 0.05;
  scene.add(moon);
  const rim = new THREE.DirectionalLight(0xff9a5c, 0.9); // 따뜻한 지평선 림라이트(티얼/오렌지 대비)
  rim.position.set(60, 8, 70);
  scene.add(rim);
  const crystalLight = new THREE.PointLight(0x7fe9ff, P.crystalLight, 60, 2);
  crystalLight.position.copy(HERO_POS);
  crystalLight.castShadow = false;
  scene.add(crystalLight);

  // 주인공 수정
  const hero = new THREE.Group(); hero.name = 'hero_crystal'; hero.position.copy(HERO_POS);
  const crystalMat = new THREE.MeshStandardMaterial({ color: 0x9ceeff, emissive: 0x38d9ff, emissiveIntensity: 2.2, roughness: P.crystalRough, metalness: 0.1, flatShading: true });
  const core = new THREE.Mesh(new THREE.OctahedronGeometry(1, 0), crystalMat);
  core.scale.set(0.95, 1.9, 0.95); core.name = 'crystal_core';
  const inner = new THREE.Mesh(new THREE.OctahedronGeometry(0.55, 0), new THREE.MeshBasicMaterial({ color: 0xeafcff }));
  inner.scale.set(0.9, 1.9, 0.9);
  hero.add(core, inner);
  const shards = [];
  for (let i = 0; i < 9; i++) {
    const s = new THREE.Mesh(new THREE.OctahedronGeometry(0.22 + rng() * 0.16, 0), crystalMat);
    s.userData = { r: 2.0 + rng() * 1.6, a0: rng() * Math.PI * 2, sp: 0.35 + rng() * 0.5, y0: (rng() - 0.5) * 2.4, tilt: (rng() - 0.5) * 0.9 };
    hero.add(s); shards.push(s);
  }
  scene.add(hero);

  let halo = null, fogSprites = [], dust = null, burst = null, beam = null;
  if (!headless) {
    const mkTex = (draw, size = 256) => { const c = document.createElement('canvas'); c.width = c.height = size; draw(c.getContext('2d'), size); const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t; };
    const haloTex = mkTex((g, s) => { const gr = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2); gr.addColorStop(0, 'rgba(180,245,255,1)'); gr.addColorStop(0.18, 'rgba(90,220,255,0.55)'); gr.addColorStop(0.5, 'rgba(40,140,220,0.14)'); gr.addColorStop(1, 'rgba(0,0,0,0)'); g.fillStyle = gr; g.fillRect(0, 0, s, s); });
    halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, fog: false }));
    halo.position.copy(HERO_POS); halo.scale.setScalar(13); halo.name = 'halo';
    scene.add(halo);

    // 안개 스프라이트(저고도 볼륨감)
    const fogTex = mkTex((g, s) => {
      const img = g.createImageData(s, s);
      for (let y = 0; y < s; y++) for (let x = 0; x < s; x++) {
        const nx = x / s - 0.5, ny = y / s - 0.5;
        const d = Math.sqrt(nx * nx + ny * ny) * 2;
        const n = fbm(x * 0.035 + 7, y * 0.035 + 3, 5);
        const a = Math.max(0, 1 - d) ** 1.4 * (0.35 + n * 0.9);
        const i = (y * s + x) * 4; img.data[i] = 150; img.data[i + 1] = 170; img.data[i + 2] = 205; img.data[i + 3] = Math.min(255, a * 255);
      }
      g.putImageData(img, 0, 0);
    }, 256);
    for (let i = 0; i < P.fogSprites; i++) {
      const m = new THREE.Sprite(new THREE.SpriteMaterial({ map: fogTex, transparent: true, opacity: P.fogSpriteOpacity * (0.6 + rng() * 0.8), depthWrite: false, color: 0xb9c8e6 }));
      const a = rng() * Math.PI * 2, r = 4 + rng() * 55;
      m.userData = { a, r, y: 0.5 + rng() * 3.6, sp: (0.01 + rng() * 0.03) * (rng() < 0.5 ? -1 : 1), s: 14 + rng() * 22 };
      m.scale.set(m.userData.s, m.userData.s * 0.45, 1);
      m.renderOrder = 5;
      scene.add(m); fogSprites.push(m);
    }

    // 떠다니는 먼지/입자: 위치는 셰이더에서 uTime 의 순수 함수
    const mkPoints = (count, { burstMode = false } = {}) => {
      const g = new THREE.BufferGeometry();
      const p = new Float32Array(count * 3), s = new Float32Array(count * 4);
      for (let i = 0; i < count; i++) {
        p[i * 3] = (rng() - 0.5) * 90; p[i * 3 + 1] = rng() * 22; p[i * 3 + 2] = (rng() - 0.5) * 90;
        s[i * 4] = rng(); s[i * 4 + 1] = rng(); s[i * 4 + 2] = rng(); s[i * 4 + 3] = rng();
      }
      g.setAttribute('position', new THREE.BufferAttribute(p, 3));
      g.setAttribute('aSeed', new THREE.BufferAttribute(s, 4));
      const mat = new THREE.ShaderMaterial({
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
        uniforms: { uTime: { value: 0 }, uPulseT: { value: -10 }, uSize: { value: P.particleSize }, uScale: { value: HEIGHT / 2 }, uHero: { value: HERO_POS.clone() }, uBurst: { value: burstMode ? 1 : 0 } },
        vertexShader: `
          attribute vec4 aSeed; uniform float uTime, uPulseT, uSize, uScale, uBurst; uniform vec3 uHero;
          varying float vA; varying vec3 vC;
          void main(){
            vec3 p = position;
            float t = uTime;
            float alpha;
            if (uBurst > 0.5) {
              float age = uTime - uPulseT; // 초
              vec3 dir = normalize(vec3(aSeed.x-0.5, aSeed.y*0.9-0.25, aSeed.z-0.5));
              float spd = 3.5 + aSeed.w*9.0;
              p = uHero + dir*spd*age*(1.0-0.35*age/(1.0+age)) + vec3(0.0,-0.9*age*age*0.5*aSeed.w,0.0);
              alpha = age < 0.0 ? 0.0 : exp(-age*0.9) * (0.4+0.6*aSeed.x);
              vC = mix(vec3(0.5,0.9,1.0), vec3(1.0,0.75,0.4), aSeed.y);
            } else {
              p.x += sin(t*0.25 + aSeed.x*6.283)*1.6 + t*0.12;
              p.z += cos(t*0.21 + aSeed.y*6.283)*1.6;
              p.y = mod(p.y + t*(0.18+aSeed.z*0.35), 22.0);
              p.x = mod(p.x + 45.0, 90.0) - 45.0;
              float fade = smoothstep(0.0,2.0,p.y)*smoothstep(22.0,16.0,p.y);
              float tw = 0.55+0.45*sin(t*(1.2+aSeed.w*2.5)+aSeed.x*40.0);
              alpha = fade*tw*(0.25+0.75*aSeed.w);
              vC = mix(vec3(0.55,0.85,1.0), vec3(1.0,0.78,0.5), step(0.8,aSeed.z));
              float dh = length(p - uHero);
              alpha *= 1.0 + 1.5*exp(-dh*0.12); // 수정 주변에서 더 밝게
            }
            vA = alpha;
            vec4 mv = modelViewMatrix*vec4(p,1.0);
            gl_Position = projectionMatrix*mv;
            gl_PointSize = clamp(uSize*(0.5+aSeed.w*1.4)*uScale/max(-mv.z,0.1)*0.35, 1.0, 26.0);
          }`,
        fragmentShader: `
          varying float vA; varying vec3 vC;
          void main(){ vec2 d = gl_PointCoord-0.5; float r = length(d)*2.0; float a = smoothstep(1.0,0.0,r); a*=a; gl_FragColor = vec4(vC*vA*a*1.4, vA*a); }`,
      });
      const pts = new THREE.Points(g, mat);
      pts.frustumCulled = false; pts.renderOrder = 8;
      return pts;
    };
    dust = mkPoints(P.particles); dust.name = 'dust';
    burst = mkPoints(900, { burstMode: true }); burst.name = 'burst'; burst.material.uniforms.uSize.value = P.burstSize;
    scene.add(dust, burst);

    // 수정 아래로 퍼지는 빛줄기(프레넬 감쇠 콘)
    beam = new THREE.Mesh(
      new THREE.CylinderGeometry(0.25, 3.2, 26, 40, 1, true),
      new THREE.ShaderMaterial({
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
        uniforms: { uI: { value: 0.1 }, uCol: { value: new THREE.Color(0x66d8ff) } },
        vertexShader: `varying vec3 vN; varying vec3 vV; varying float vY; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); vY = uv.y; gl_Position = projectionMatrix*mv; }`,
        fragmentShader: `varying vec3 vN; varying vec3 vV; varying float vY; uniform float uI; uniform vec3 uCol; void main(){ float f = pow(1.0-abs(dot(normalize(vN),normalize(vV))),1.6); gl_FragColor = vec4(uCol*uI*f*(0.2+0.8*vY), uI*f*vY); }`,
      })
    );
    beam.position.set(0, HERO_POS.y - 13 + 1.0, 0); beam.renderOrder = 6;
    scene.add(beam);
  }

  // 프레임 → 장면 상태 (순수 함수)
  function update(frame) {
    const t = frame / FPS;
    const pu = pulseAt(frame);
    hero.rotation.y = t * 0.55;
    hero.position.y = HERO_POS.y + Math.sin(t * 1.1) * 0.22;
    crystalMat.emissiveIntensity = P.emissiveBase + pu * P.emissivePulse;
    crystalLight.intensity = P.crystalLight * (0.75 + pu * (P.runes ? 0.55 : 1.1)) * (1 + 0.04 * Math.sin(t * 17.0));
    crystalLight.position.copy(hero.position);
    shards.forEach((s, i) => {
      const d = s.userData, a = d.a0 + t * d.sp;
      s.position.set(Math.cos(a) * d.r, d.y0 + Math.sin(a * 1.7 + i) * 0.35 + Math.sin(a) * d.tilt * d.r * 0.4, Math.sin(a) * d.r);
      s.rotation.set(t * 0.9 + i, t * 0.7 + i * 2, 0);
    });
    ring.material.color.setScalar(1).multiplyScalar(0.6 + pu * 0.7).multiply(new THREE.Color(0x8be9ff));
    ring.rotation.z = t * 0.2;
    runes.forEach((r) => { const k = 0.35 + 1.9 * Math.max(0, pulseAt(frame - r.delay) - 0.2); r.mat.color.setRGB(0.18, 0.78, 1.0).multiplyScalar(k); });
    if (!headless) {
      halo.position.copy(hero.position);
      halo.scale.setScalar(P.haloBase + pu * P.haloPulse);
      halo.material.opacity = 0.5 + 0.3 * Math.min(1, pu);
      dust.material.uniforms.uTime.value = t;
      burst.material.uniforms.uTime.value = t;
      burst.material.uniforms.uPulseT.value = PULSE_FRAME / FPS;
      beam.material.uniforms.uI.value = P.beamBase + P.beamPulse * Math.min(1.5, pu);
      fogSprites.forEach((m) => {
        const d = m.userData; const a = d.a + t * d.sp;
        m.position.set(Math.cos(a) * d.r, heightAt(Math.cos(a) * d.r, Math.sin(a) * d.r) + d.y, Math.sin(a) * d.r);
      });
    }
  }

  return { scene, solids, boxes, pillars, terrain, hero, update, params: P, heroPosAt: (frame) => hero.position.clone() };
}
