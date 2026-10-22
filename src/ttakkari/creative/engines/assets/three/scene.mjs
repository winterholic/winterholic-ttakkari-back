// 프레임 결정적 Three 장면. 같은 spec 과 프레임 번호는 렌더 순서와 상관없이 같은 화면을 만든다.
import * as THREE from "three";

const spec = await (await fetch("/spec.json")).json();
const { width, height, fps, seed } = spec;

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const palette = (spec.palette && spec.palette.length ? spec.palette : ["#101418", "#f4f4ed", "#00f0b5"]).map((c) => new THREE.Color(c));
const rand = mulberry32(seed >>> 0);

const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(width, height);
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = palette[0].clone();
scene.fog = new THREE.Fog(palette[0].clone(), 6, 22);
const camera = new THREE.PerspectiveCamera(spec.camera?.fov ?? 45, width / height, 0.1, 100);

scene.add(new THREE.HemisphereLight(0xffffff, palette[0].getHex(), 0.9));
const key = new THREE.DirectionalLight(0xffffff, 2.2);
key.position.set(4, 6, 5);
scene.add(key);

const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ color: palette[0].clone().lerp(new THREE.Color(0xffffff), 0.12), roughness: 0.9 }));
floor.rotation.x = -Math.PI / 2;
scene.add(floor);

const count = Math.max(3, Math.min(spec.objects ?? 7, 40));
const items = [];
for (let i = 0; i < count; i++) {
  const geometry = [new THREE.BoxGeometry(1, 1, 1), new THREE.SphereGeometry(0.6, 24, 16), new THREE.TorusGeometry(0.5, 0.2, 12, 28)][Math.floor(rand() * 3)];
  const color = palette[1 + (i % Math.max(1, palette.length - 1))] ?? palette[0];
  const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: color.clone(), roughness: 0.45, metalness: 0.1 }));
  const angle = (i / count) * Math.PI * 2;
  const radius = 2 + rand() * 2.5;
  mesh.position.set(Math.cos(angle) * radius, 0.6 + rand() * 1.2, Math.sin(angle) * radius);
  mesh.userData = { spin: 0.4 + rand() * 1.2, bob: rand() * Math.PI * 2 };
  scene.add(mesh);
  items.push(mesh);
}

const cues = spec.cue_frames ?? [];
const move = spec.camera?.move ?? "orbit";
const total = Math.max(1, spec.frames - 1);

function frameState(i) {
  const t = i / fps;
  const u = i / total;
  for (const m of items) {
    m.rotation.y = m.userData.spin * t;
    m.rotation.x = m.userData.spin * 0.5 * t;
  }
  // 위치는 프레임 번호의 절대식으로만 정한다(누적하면 렌더 순서에 결과가 의존한다).
  items.forEach((m, k) => {
    const base = 0.6 + ((k * 0.37) % 1.2);
    m.position.y = base + Math.sin(t * 2 + m.userData.bob) * 0.15;
  });
  let pulse = 0;
  for (const c of cues) pulse += Math.max(0, 1 - Math.abs(i - c) / 6);
  const orbit = (spec.camera?.degrees ?? 90) * Math.PI / 180;
  const dist = spec.camera?.distance ?? 9;
  const height_ = spec.camera?.height ?? 3.2;
  if (move === "dolly") {
    camera.position.set(0, height_, dist * (1.15 - 0.45 * u));
  } else {
    const a = orbit * u;
    camera.position.set(Math.sin(a) * dist, height_, Math.cos(a) * dist);
  }
  camera.lookAt(0, 1, 0);
  camera.fov = (spec.camera?.fov ?? 45) - Math.min(pulse, 1) * 3;
  camera.updateProjectionMatrix();
  key.intensity = 2.2 + Math.min(pulse, 1) * 0.8;
}

window.renderFrame = (i) => {
  frameState(i);
  renderer.render(scene, camera);
  return renderer.domElement.toDataURL("image/png");
};
window.__info = () => {
  const gl = renderer.getContext();
  const ext = gl.getExtension("WEBGL_debug_renderer_info");
  return { gl_renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER), three: THREE.REVISION, objects: items.length };
};
window.__ready = true;
