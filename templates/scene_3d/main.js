// 브라우저 렌더 진입점. window.renderFrame(i) 가 i 번째 프레임을 그리고 PNG dataURL 을 돌려준다.
import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { BokehPass } from 'three/addons/postprocessing/BokehPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
const q = new URLSearchParams(location.search);
// ?world=world.v1.snapshot.js 로 v1 코드를 그대로 재현할 수 있다(전후 비교의 재현성)
const { PARAMS, WIDTH, HEIGHT, FPS, TOTAL_FRAMES, buildWorld, makeShots, cameraAt, pulseAt } = await import('./' + (q.get('world') || 'world.js'));
const ver = q.get('v') || 'v2';
const P = PARAMS[ver];
const W = +(q.get('w') || WIDTH), H = +(q.get('h') || HEIGHT);

const renderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(1);
renderer.setSize(W, H);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = P.exposure;
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.style.margin = '0';
document.body.appendChild(renderer.domElement);

const world = buildWorld(P);
const shots = makeShots(P);
const camera = new THREE.PerspectiveCamera(35, W / H, 0.1, 900);

const rt = new THREE.WebGLRenderTarget(W, H, { type: THREE.HalfFloatType, samples: 4 });
const composer = new EffectComposer(renderer, rt);
composer.setSize(W, H);
composer.addPass(new RenderPass(world.scene, camera));
// 안전 패스: HalfFloat 오버플로(inf)·NaN 이 bloom 으로 번져 프레임 전체가 검게 되는 것을 막는다(v1 에서 5프레임 발생)
if (q.get('safe') !== '0') {
  composer.addPass(new ShaderPass({
    uniforms: { tDiffuse: { value: null } },
    vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }',
    fragmentShader: 'uniform sampler2D tDiffuse; varying vec2 vUv; void main(){ vec4 c = texture2D(tDiffuse, vUv); if (any(isnan(c.rgb)) || any(isinf(c.rgb))) c = vec4(0.0,0.0,0.0,1.0); c.rgb = min(c.rgb, vec3(48.0)); gl_FragColor = c; }',
  }));
}
const bokeh = new BokehPass(world.scene, camera, { focus: 20, aperture: P.dofAperture, maxblur: P.dofMaxBlur });
composer.addPass(bokeh);
const bloom = new UnrealBloomPass(new THREE.Vector2(W, H), P.bloom[0], P.bloom[1], P.bloom[2]);
composer.addPass(bloom);
composer.addPass(new OutputPass());

// 최종 룩: 색수차 + 비네트 + 필름 그레인 + 틸/오렌지 그레이드 + 레터박스 + 페이드
const finalPass = new ShaderPass({
  uniforms: { tDiffuse: { value: null }, uFrame: { value: 0 }, uGrain: { value: P.grain }, uVig: { value: P.vignette }, uCA: { value: P.ca }, uLetter: { value: P.letterbox }, uFade: { value: 1 }, uAspect: { value: W / H } },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }`,
  fragmentShader: `
    uniform sampler2D tDiffuse; uniform float uFrame, uGrain, uVig, uCA, uLetter, uFade, uAspect; varying vec2 vUv;
    float h(vec2 p){ p = fract(p*vec2(443.897,441.423)); p += dot(p,p+19.19); return fract((p.x+p.y)*p.x); }
    void main(){
      vec2 c = vUv-0.5; float r2 = dot(c,c);
      vec2 off = c*uCA*(1.0+r2*3.0);
      vec3 col = vec3(texture2D(tDiffuse, vUv+off).r, texture2D(tDiffuse, vUv).g, texture2D(tDiffuse, vUv-off).b);
      // 그레이드: 그림자 틸, 하이라이트 따뜻하게, 약한 S 커브
      float l = dot(col, vec3(0.299,0.587,0.114));
      col = mix(col, col*vec3(0.88,1.0,1.10), smoothstep(0.5,0.0,l)*0.55);
      col = mix(col, col*vec3(1.08,1.0,0.90), smoothstep(0.45,1.0,l)*0.5);
      col = mix(col, col*col*(3.0-2.0*col), 0.35);
      col *= 1.0 - uVig*smoothstep(0.18,0.62,r2*1.6);
      float g = h(vUv*vec2(1920.0,1080.0) + uFrame*17.31) - 0.5;
      col += g*uGrain*(1.0 - 0.6*l);
      float lb = step(uLetter, vUv.y) * step(vUv.y, 1.0-uLetter);
      col *= lb;
      col *= uFade;
      gl_FragColor = vec4(col,1.0);
    }`,
});
composer.addPass(finalPass);

function fadeAt(frame) {
  const fin = Math.min(1, frame / 18);
  const fout = Math.min(1, (TOTAL_FRAMES - 1 - frame) / 14);
  return Math.max(0, Math.min(fin, fout));
}

window.renderFrame = (frame, asData = true) => {
  const cs = cameraAt(shots, frame);
  camera.position.copy(cs.pos);
  camera.up.set(0, 1, 0);
  camera.lookAt(cs.target);
  camera.rotateZ(cs.roll);
  camera.fov = cs.fov; camera.updateProjectionMatrix();
  world.update(frame);
  const pu = pulseAt(frame);
  bokeh.uniforms.focus.value = cs.focus;
  bokeh.uniforms.aperture.value = P.dofAperture * cs.dofOn;
  bokeh.uniforms.maxblur.value = P.dofMaxBlur;
  bloom.strength = P.bloom[0] * (0.85 + 0.55 * Math.min(1.5, pu));
  finalPass.uniforms.uFrame.value = frame;
  finalPass.uniforms.uFade.value = fadeAt(frame);
  composer.render();
  return asData ? renderer.domElement.toDataURL('image/png') : null;
};
window.__info = () => ({ ver, W, H, FPS, TOTAL_FRAMES, gl: renderer.getContext().getParameter(renderer.getContext().VERSION), calls: renderer.info.render.calls, tris: renderer.info.render.triangles });
// 디버그: 패스 토글(검은 프레임 원인 추적용)
window.__toggle = (name, on) => { ({ bokeh, bloom })[name].enabled = on; };
window.__cam = (frame) => { const c = cameraAt(shots, frame); return { pos: c.pos.toArray(), target: c.target.toArray(), fov: c.fov, roll: c.roll, focus: c.focus, dofOn: c.dofOn }; };
window.__ready = true;
