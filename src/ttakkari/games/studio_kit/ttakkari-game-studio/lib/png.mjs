// 이미지 헤더(PNG/JPEG/GIF)와 PNG 화소 통계. 의존성 없음.
// 화소 통계는 8비트 비인터레이스 PNG 만 읽는다. 그 밖(JPEG·WebP·16비트)은 pixels=null 로 두고 '분석 안 함' 이라 말한다.
import zlib from 'node:zlib';

const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

export function imageKind(buf) {
  if (buf.length >= 8 && buf.subarray(0, 8).equals(PNG_SIG)) return 'png';
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8) return 'jpeg';
  if (buf.length >= 6 && buf.subarray(0, 3).toString() === 'GIF') return 'gif';
  if (buf.length >= 12 && buf.subarray(0, 4).toString() === 'RIFF' && buf.subarray(8, 12).toString() === 'WEBP') return 'webp';
  return null;
}

function jpegSize(buf) {
  let i = 2;
  while (i + 9 < buf.length) {
    if (buf[i] !== 0xff) {
      i++;
      continue;
    }
    const m = buf[i + 1];
    if (m === 0xd8 || (m >= 0xd0 && m <= 0xd7) || m === 0x01) {
      i += 2;
      continue;
    }
    const len = buf.readUInt16BE(i + 2);
    if (m >= 0xc0 && m <= 0xcf && m !== 0xc4 && m !== 0xc8 && m !== 0xcc) return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
    i += 2 + len;
  }
  return null;
}

export function imageSize(buf) {
  const k = imageKind(buf);
  if (k === 'png') return { kind: k, width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  if (k === 'jpeg') return { kind: k, ...(jpegSize(buf) ?? { width: null, height: null }) };
  if (k === 'gif') return { kind: k, width: buf.readUInt16LE(6), height: buf.readUInt16LE(8) };
  if (k === 'webp') return { kind: k, width: null, height: null };
  return { kind: null, width: null, height: null };
}

const CHANNELS = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 };

/** 비인터레이스 8비트 PNG 를 RGBA 로 푼다. 지원 밖이면 null. */
export function decodePng(buf) {
  if (imageKind(buf) !== 'png') return null;
  let off = 8;
  let ihdr = null;
  let plte = null;
  let trns = null;
  const idat = [];
  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.subarray(off + 4, off + 8).toString('latin1');
    const data = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') ihdr = { w: data.readUInt32BE(0), h: data.readUInt32BE(4), depth: data[8], ct: data[9], inter: data[12] };
    else if (type === 'PLTE') plte = data;
    else if (type === 'tRNS') trns = data;
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    off += 12 + len;
  }
  if (!ihdr || ihdr.depth !== 8 || ihdr.inter !== 0 || !(ihdr.ct in CHANNELS)) return null;
  const { w, h, ct } = ihdr;
  const bpp = CHANNELS[ct];
  let raw;
  try {
    raw = zlib.inflateSync(Buffer.concat(idat));
  } catch {
    return null;
  }
  const stride = w * bpp;
  if (raw.length < (stride + 1) * h) return null;
  const px = Buffer.alloc(stride * h);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)];
    const src = y * (stride + 1) + 1;
    const dst = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? px[dst + x - bpp] : 0;
      const b = y > 0 ? px[dst - stride + x] : 0;
      const c = x >= bpp && y > 0 ? px[dst - stride + x - bpp] : 0;
      let v = raw[src + x];
      if (f === 1) v += a;
      else if (f === 2) v += b;
      else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (f !== 0) return null;
      px[dst + x] = v & 255;
    }
  }
  const rgba = Buffer.alloc(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    let r;
    let g;
    let b;
    let a = 255;
    if (ct === 0) r = g = b = px[i];
    else if (ct === 2) [r, g, b] = [px[i * 3], px[i * 3 + 1], px[i * 3 + 2]];
    else if (ct === 3) {
      const idx = px[i];
      if (!plte) return null;
      [r, g, b] = [plte[idx * 3], plte[idx * 3 + 1], plte[idx * 3 + 2]];
      if (trns && idx < trns.length) a = trns[idx];
    } else if (ct === 4) {
      r = g = b = px[i * 2];
      a = px[i * 2 + 1];
    } else [r, g, b, a] = [px[i * 4], px[i * 4 + 1], px[i * 4 + 2], px[i * 4 + 3]];
    rgba[i * 4] = r;
    rgba[i * 4 + 1] = g;
    rgba[i * 4 + 2] = b;
    rgba[i * 4 + 3] = a;
  }
  return { width: w, height: h, rgba };
}

/** 화소 통계: 색 버킷 수, 가장 흔한 색의 비율, 투명 비율. 큰 이미지는 격자로 표본 추출한다. */
export function pixelStats(img) {
  const { width: w, height: h, rgba } = img;
  const stepX = Math.max(1, Math.floor(w / 256));
  const stepY = Math.max(1, Math.floor(h / 256));
  const counts = new Map();
  let n = 0;
  let transparent = 0;
  for (let y = 0; y < h; y += stepY) {
    for (let x = 0; x < w; x += stepX) {
      const i = (y * w + x) * 4;
      n++;
      if (rgba[i + 3] < 16) {
        transparent++;
        continue;
      }
      const k = (rgba[i] >> 4) * 256 + (rgba[i + 1] >> 4) * 16 + (rgba[i + 2] >> 4);
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
  }
  const opaque = n - transparent;
  const top = counts.size ? Math.max(...counts.values()) : 0;
  return {
    sampled: n,
    buckets: counts.size,
    dominantFraction: opaque ? Math.round((top / opaque) * 1000) / 1000 : 0,
    transparentFraction: Math.round((transparent / n) * 1000) / 1000,
  };
}

/** PNG(Buffer) → 판정. blank = 화면 전체가 한 색이거나 완전 투명. */
export function analyzePng(buf) {
  const img = decodePng(buf);
  if (!img) return null;
  const s = pixelStats(img);
  return { ...s, blank: s.transparentFraction > 0.995 || (s.transparentFraction < 0.005 && s.buckets <= 1) };
}
