// Resolves design tokens (CSS custom properties, OKLCH) to RGBA bytes for WebGL.
// Map colours therefore come from the same tokens as the chrome, in both themes.

import type { LegMode } from './types';

export type RGBA = [number, number, number, number];

export interface Palette {
  mode: Record<LegMode, RGBA>;
  danger: RGBA;
  warning: RGBA;
  neutral: RGBA;
  surface: RGBA;
  text: RGBA;
  textSecondary: RGBA;
  canvas: RGBA;
  dark: boolean;
}

let ctx: CanvasRenderingContext2D | null = null;

function toRGBA(css: string): RGBA {
  if (!ctx) {
    const c = document.createElement('canvas');
    c.width = c.height = 1;
    ctx = c.getContext('2d', { willReadFrequently: true });
  }
  if (!ctx) return [128, 128, 128, 255];
  ctx.clearRect(0, 0, 1, 1);
  ctx.fillStyle = '#808080';
  ctx.fillStyle = css;
  ctx.fillRect(0, 0, 1, 1);
  const d = ctx.getImageData(0, 0, 1, 1).data;
  return [d[0], d[1], d[2], d[3]];
}

/** Read token values as resolved on `el` (the themed module root). */
export function readPalette(el: HTMLElement, dark: boolean): Palette {
  const probe = document.createElement('span');
  probe.style.display = 'none';
  el.appendChild(probe);
  const get = (token: string): RGBA => {
    probe.style.color = `var(${token})`;
    return toRGBA(getComputedStyle(probe).color);
  };
  const p: Palette = {
    mode: {
      sea: get('--huo-mode-sea'),
      road: get('--huo-mode-road'),
      air: get('--huo-mode-air'),
      rail: get('--huo-mode-rail'),
    },
    danger: get('--tone-danger-dot'),
    warning: get('--tone-warning-dot'),
    neutral: get('--text-tertiary'),
    surface: get('--bg-surface'),
    text: get('--text-primary'),
    textSecondary: get('--text-secondary'),
    canvas: get('--bg-canvas'),
    dark,
  };
  probe.remove();
  return p;
}

export function rgbaCss(c: RGBA, a = c[3] / 255): string {
  return `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a})`;
}
