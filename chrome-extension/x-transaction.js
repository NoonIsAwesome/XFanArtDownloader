/* SPDX-License-Identifier: GPL-2.0-only
 * Copyright 2025-2026 Mike Fährmann
 * JavaScript adaptation for FanArtDownloader, 2026.
 * Derived from gallery-dl/extractor/utils/twitter_transaction_id.py (GPL v2),
 * itself adapted from https://github.com/iSarabjitDhiman/XClientTransaction.
 * Remote assets are parsed as data; their JavaScript is never executed.
 */

export function roundEven(value, digits = 0) {
  if (!Number.isFinite(value) || value === 0) return value;
  const buffer = new ArrayBuffer(8), view = new DataView(buffer);
  view.setFloat64(0, Math.abs(value));
  const bits = view.getBigUint64(0), exponent = Number((bits >> 52n) & 2047n);
  let numerator = bits & ((1n << 52n) - 1n);
  if (exponent) numerator |= 1n << 52n;
  const power = (exponent || 1) - 1023 - 52;
  let denominator = 1n;
  if (power >= 0) numerator <<= BigInt(power); else denominator <<= BigInt(-power);
  numerator *= 10n ** BigInt(digits);
  let quotient = numerator / denominator;
  const remainder = numerator % denominator;
  if (remainder * 2n > denominator || (remainder * 2n === denominator && quotient % 2n)) quotient++;
  return Math.sign(value) * Number(quotient) / (10 ** digits);
}

const scale = (value, low, high, floor = false) => {
  const result = value * (high - low) / 255 + low;
  return floor ? Math.floor(result) : roundEven(result, 2);
};
const cubic = (a, b, m) => 3 * a * (1 - m) ** 2 * m + 3 * b * (1 - m) * m ** 2 + m ** 3;

export function cubicValue(curve, t) {
  if (curve.length !== 4 || !curve.every(Number.isFinite)) throw new Error('Invalid animation curve');
  if (t <= 0) {
    const slope = curve[0] > 0 ? curve[1] / curve[0] : curve[1] === 0 && curve[2] > 0 ? curve[3] / curve[2] : 0;
    return slope * t;
  }
  if (t >= 1) {
    const slope = curve[2] < 1 ? (curve[3] - 1) / (curve[2] - 1) : curve[2] === 1 && curve[0] < 1 ? (curve[1] - 1) / (curve[0] - 1) : 0;
    return 1 + slope * (t - 1);
  }
  let start = 0, end = 1, mid = 0;
  for (let i = 0; i < 80; i++) {
    mid = (start + end) / 2;
    const estimate = cubic(curve[0], curve[2], mid);
    if (Math.abs(t - estimate) < 0.00001) break;
    if (estimate < t) start = mid; else end = mid;
  }
  return cubic(curve[1], curve[3], mid);
}

export function animationKey(row, time) {
  if (row.length !== 11 || !row.every(Number.isFinite)) throw new Error('Invalid animation row');
  const curve = row.slice(7).map((value, index) => scale(value, index % 2 ? -1 : 0, 1));
  const progress = cubicValue(curve, time);
  const colors = row.slice(0, 3).map((value, index) => roundEven(Math.min(255, Math.max(0, value * (1 - progress) + row[index + 3] * progress))).toString(16));
  const angle = scale(row[6], 60, 360, true) * progress * Math.PI / 180;
  const cos = Math.cos(angle), sin = Math.sin(angle);
  const matrix = [cos, -sin, sin, cos].map(value => Math.abs(roundEven(value, 2)).toString(16));
  return [...colors, ...matrix, '00'].join('').replace(/[.\-]/g, '');
}

function attribute(tag, name) {
  return tag.match(new RegExp(`\\b${name}\\s*=\\s*["']([^"']*)["']`, 'i'))?.[1];
}

export function extractBootstrap(html) {
  const meta = [...html.matchAll(/<meta\b[^>]*>/gi)].map(match => match[0]).find(tag => attribute(tag, 'name') === 'twitter-site-verification');
  const verification = meta && attribute(meta, 'content');
  const frames = [...html.matchAll(/<svg\b[^>]*\bid=["']loading-x-anim-[^"']+["'][^>]*>[\s\S]*?<\/svg>/gi)].map(match => match[0]);
  let asset = html.match(/https:\/\/abs\.twimg\.com\/responsive-web\/client-web(?:-legacy)?\/ondemand\.s\.[A-Za-z0-9_-]+\.js/)?.[0];
  if (!asset) {
    const chunk = html.match(/(?:^|[,{])\s*["']?(\d+)["']?\s*:\s*["']ondemand\.s["']/)?.[1];
    if (chunk) {
      const remaining = html.slice(html.indexOf('ondemand.s') + 'ondemand.s'.length);
      const hash = remaining.match(new RegExp(`["']?${chunk}["']?\\s*:\\s*["']([A-Za-z0-9_-]+)["']`))?.[1];
      if (hash) asset = `https://abs.twimg.com/responsive-web/client-web/ondemand.s.${hash}a.js`;
    }
  }
  if (!verification || !asset || frames.length < 4) throw new Error('X transaction bootstrap changed');
  return { verification, frames, asset };
}

export function extractIndices(script) {
  const indices = [...script.matchAll(/\(\w+\[(\d{1,2})\],\s*16\)/g)].map(match => Number(match[1]));
  if (indices.length < 2) throw new Error('X transaction indices changed');
  return indices;
}

export class XTransaction {
  constructor(keyBytes, animation, { now = () => Date.now(), random = () => crypto.getRandomValues(new Uint8Array(1))[0] & 15, subtle = globalThis.crypto?.subtle } = {}) {
    this.keyBytes = Uint8Array.from(keyBytes);
    this.animation = animation;
    this.now = now;
    this.random = random;
    this.subtle = subtle;
  }

  static async fromHomepage(html, fetchText, options = {}) {
    const { verification, frames, asset } = extractBootstrap(html);
    const indices = extractIndices(await fetchText(asset));
    let key;
    try { key = Uint8Array.from(atob(verification), char => char.charCodeAt(0)); } catch { throw new Error('Invalid X verification data'); }
    if (key.length < 6 || indices.some(index => index >= key.length)) throw new Error('Invalid X verification indices');
    const frame = frames[key[5] % 4];
    const paths = [...frame.matchAll(/<path\b[^>]*\bd=["']([^"']+)["']/gi)].map(match => match[1]);
    const path = paths[1];
    if (!path) throw new Error('X animation path changed');
    const rows = path.slice(9).split('C').map(part => (part.match(/\d+/g) || []).map(Number));
    const row = rows[key[indices[0]] % 16];
    let frameTime = 1;
    for (const index of indices.slice(1)) frameTime *= key[index] % 16;
    const targetTime = Math.round(frameTime / 10) * 10 / 4096;
    if (!row) throw new Error('X animation frames changed');
    return new XTransaction(key, animationKey(row, targetTime), options);
  }

  async generate(method, path) {
    if (!this.subtle) throw new Error('WebCrypto unavailable');
    const milliseconds = this.now(), seconds = Math.floor(milliseconds / 1000);
    const time = seconds - 1682924400;
    const payload = `${method.toUpperCase()}!${path}!${time}obfiowerehiring${this.animation}`;
    const digest = new Uint8Array(await this.subtle.digest('SHA-256', new TextEncoder().encode(payload))).slice(0, 16);
    const mask = ((this.random() & 15) << 4) + Math.floor((milliseconds % 1000) * 16 / 1000);
    const data = [0, ...this.keyBytes, time & 255, (time >> 8) & 255, (time >> 16) & 255, (time >> 24) & 255, ...digest, 3].map(value => value ^ mask);
    return btoa(String.fromCharCode(...data));
  }
}
