/* ============================================================
   小幽 XIAOYOU · vococo 吉祥物像素引擎
   24x28 基底网格,全部状态从同一 NEUTRAL 轮廓派生(平移/缩放 + 部件叠加)
   颜色走 CSS 变量 var(--pal-*),换肤只需改 .voco-mascot 上的变量
   设计规范见 docs/design/mascot-xiaoyou.md;来源 vococo-mascots.html 展示页引擎精简版
   ============================================================ */
(function () {
  "use strict";
  const CW = 24, CH = 28;
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  const NEUTRAL = (() => {
    const p = new Array(CH).fill(null);
    p[5] = [11, 12]; p[6] = [10, 13]; p[7] = [9, 14]; p[8] = [8, 15]; p[9] = [7, 16];
    p[10] = [6, 17]; p[11] = [6, 17]; p[12] = [6, 17]; p[13] = [6, 17];
    p[14] = [7, 16]; p[15] = [7, 16]; p[16] = [7, 16];
    p[17] = [6, 17]; p[18] = [5, 18]; p[19] = [5, 18];
    return p;
  })();
  const HL = [[7, 9], [8, 8], [8, 9], [9, 7], [9, 8], [10, 6], [10, 7]];
  const SH = [[15, 16], [16, 16], [17, 17], [18, 17], [18, 18], [19, 18]];

  function eyeCells(state) {
    const out = [];
    if (state === 'open') {
      for (const row of [12, 13]) for (const c of [9, 10, 13, 14]) out.push([row, c]);
    } else if (state === 'closed') {
      for (const c of [9, 10, 13, 14]) out.push([13, c]);
    } else if (state === 'x') {
      out.push([12, 8], [12, 10], [13, 9], [14, 8], [14, 10]);
      out.push([12, 13], [12, 15], [13, 14], [14, 13], [14, 15]);
    }
    return out;
  }

  const TOES = [[5, 6], [8, 9], [11, 12], [14, 15], [17, 18]];
  const FEET = {
    waveA: (() => { const c = []; TOES.forEach(([a, b]) => c.push([20, a], [20, b])); c.push([21, 8], [21, 9], [21, 14], [21, 15]); return c; })(),
    waveB: (() => { const c = []; TOES.forEach(([a, b]) => c.push([20, a], [20, b])); c.push([21, 5], [21, 6], [21, 11], [21, 12], [21, 17], [21, 18]); return c; })(),
    tuck: [[20, 8], [20, 9], [20, 11], [20, 12], [20, 14], [20, 15]],
    wide: (() => { const c = []; [[4, 6], [8, 9], [11, 12], [14, 15], [17, 19]].forEach(([a, b]) => { for (let x = a; x <= b; x++) { c.push([20, x]); c.push([21, x]); } }); return c; })(),
    droop: [[21, 8], [21, 9], [21, 11], [21, 12], [21, 14], [21, 15]],
    flat: [[20, 8], [20, 9], [20, 10], [20, 13], [20, 14], [20, 15]]
  };

  const BAR_COLS = [[6, 7], [9, 10], [12, 13], [15, 16], [18, 19]];
  function barCells(heights) {
    const out = [];
    heights.forEach((h, i) => {
      const [a, b] = BAR_COLS[i];
      for (let row = 28 - h; row <= 27; row++) for (let x = a; x <= b; x++) out.push([row, x]);
    });
    return out;
  }
  function dotCells(active) {
    const cols = [8, 12, 16], out = [];
    cols.forEach((c, i) => {
      const rows = i === active ? [0, 1] : [2, 3];
      rows.forEach(r => out.push([r, c], [r, c + 1]));
    });
    return out;
  }
  function zGlyph(col) {
    return [[0, col], [0, col + 1], [0, col + 2], [1, col + 2], [2, col + 1], [3, col], [4, col], [4, col + 1], [4, col + 2]];
  }
  const Z1 = zGlyph(16), Z2 = zGlyph(18), Z3 = zGlyph(19);

  function frameShadow(opts) {
    const { dy = 0, dx = 0, scale = 1, eyes = 'open', feet = 'waveA', bars = null, dots = null, zzz = null, hl = true, sh = true } = opts;
    let profile = NEUTRAL.map(r => r ? [r[0], r[1]] : null);
    if (scale !== 1) {
      profile = profile.map(r => {
        if (!r) return null;
        const mid = (r[0] + r[1]) / 2, halfw = (r[1] - r[0]) / 2 * scale;
        return [clamp(Math.round(mid - halfw), 0, CW - 1), clamp(Math.round(mid + halfw), 0, CW - 1)];
      });
    }
    const map = new Map();
    const setCell = (row, col, ch) => { if (row < 0 || row >= CH || col < 0 || col >= CW) return; map.set(row + ',' + col, ch); };
    profile.forEach((r, row) => { if (!r) return; for (let c = r[0]; c <= r[1]; c++) setCell(row + dy, c + dx, 'p'); });
    if (hl) HL.forEach(([r, c]) => setCell(r + dy, c + dx, 'w'));
    if (sh) SH.forEach(([r, c]) => setCell(r + dy, c + dx, 'd'));
    FEET[feet].forEach(([r, c]) => setCell(r + dy, c + dx, 'p'));
    eyeCells(eyes).forEach(([r, c]) => setCell(r + dy, c + dx, 'k'));
    if (bars) barCells(bars).forEach(([r, c]) => setCell(r, c, 'p'));
    if (dots != null) dotCells(dots).forEach(([r, c]) => setCell(r, c, 'k'));
    if (zzz) zzz.forEach(([r, c]) => setCell(r, c, 'k'));
    const parts = [];
    map.forEach((ch, key) => { const [row, col] = key.split(','); parts.push(col + 'px ' + row + 'px 0 0 var(--pal-' + ch + ')'); });
    return parts.join(',');
  }

  function iconShadow() {
    const rows = { 2: [5, 8], 3: [4, 9], 4: [3, 10], 5: [3, 10], 6: [2, 11], 7: [2, 11], 8: [2, 11], 9: [2, 11], 10: [3, 10], 11: [3, 10], 12: [4, 9] };
    const map = new Map();
    Object.entries(rows).forEach(([row, [a, b]]) => { for (let c = a; c <= b; c++) map.set(row + ',' + c, 'p'); });
    [[7, 4], [7, 5], [7, 8], [7, 9]].forEach(([r, c]) => map.set(r + ',' + c, 'k'));
    const parts = [];
    map.forEach((ch, key) => { const [r, c] = key.split(','); parts.push(c + 'px ' + r + 'px 0 0 var(--pal-' + ch + ')'); });
    return parts.join(',');
  }

  const STATES = {
    idle: {
      dur: '4.8s', frames: [
        frameShadow({ dy: 0, eyes: 'open', feet: 'waveA' }),
        frameShadow({ dy: -1, eyes: 'open', feet: 'waveB' }),
        frameShadow({ dy: 0, eyes: 'open', feet: 'waveA' }),
        frameShadow({ dy: -1, eyes: 'open', feet: 'waveB' }),
        frameShadow({ dy: 0, eyes: 'open', feet: 'waveA' }),
        frameShadow({ dy: -1, eyes: 'closed', feet: 'waveB' })
      ]
    },
    listening: {
      dur: '1.0s', frames: [
        frameShadow({ dy: 0, eyes: 'open', feet: 'waveA', bars: [2, 4, 6, 4, 2] }),
        frameShadow({ dy: -1, eyes: 'open', feet: 'waveB', bars: [4, 6, 3, 6, 4] }),
        frameShadow({ dy: 0, eyes: 'open', feet: 'waveA', bars: [6, 3, 5, 3, 6] }),
        frameShadow({ dy: -1, eyes: 'open', feet: 'waveB', bars: [3, 5, 2, 5, 3] })
      ]
    },
    thinking: {
      dur: '1.2s', frames: [
        frameShadow({ eyes: 'open', feet: 'waveA', dots: 0 }),
        frameShadow({ eyes: 'open', feet: 'waveA', dots: 1 }),
        frameShadow({ eyes: 'open', feet: 'waveA', dots: 2 })
      ]
    },
    busy: {
      dur: '.6s', frames: [
        frameShadow({ dx: -2, eyes: 'open', feet: 'waveA' }),
        frameShadow({ dx: 0, eyes: 'open', feet: 'waveA' }),
        frameShadow({ dx: 2, eyes: 'open', feet: 'waveA' })
      ]
    },
    done: {
      dur: '.9s', frames: [
        frameShadow({ dy: 1, scale: 1.12, feet: 'wide', eyes: 'open' }),
        frameShadow({ dy: -4, scale: .85, feet: 'tuck', eyes: 'open' }),
        frameShadow({ dy: 1, scale: 1.15, feet: 'wide', eyes: 'open' })
      ]
    },
    err: {
      dur: '3.2s', frames: [
        frameShadow({ dy: 2, feet: 'droop', eyes: 'x' }),
        frameShadow({ dy: 3, feet: 'droop', eyes: 'x' })
      ]
    },
    sleep: {
      dur: '3.6s', frames: [
        frameShadow({ dy: 0, feet: 'flat', eyes: 'closed', zzz: Z1 }),
        frameShadow({ dy: 1, feet: 'flat', eyes: 'closed', zzz: Z2 }),
        frameShadow({ dy: 0, feet: 'flat', eyes: 'closed', zzz: Z3 })
      ]
    },
    icon: { dur: '1s', frames: [iconShadow()] }
  };

  let css = '';
  Object.entries(STATES).forEach(([name, def]) => {
    const n = def.frames.length, stepPct = 100 / n;
    let body = '';
    def.frames.forEach((f, i) => { body += (i * stepPct).toFixed(3) + '% { box-shadow:' + f + '; }\n'; });
    body += '100% { box-shadow:' + def.frames[0] + '; }\n';
    css += '@keyframes vm-' + name + ' {\n' + body + '}\n';
    css += '.voco-mascot[data-state="' + name + '"] i.vmi { animation: vm-' + name + ' ' + def.dur + ' steps(1,end) infinite backwards; }\n';
  });
  const styleTag = document.createElement('style');
  styleTag.setAttribute('data-voco-mascot', '');
  styleTag.textContent = css;
  document.head.appendChild(styleTag);

  function mount(el) {
    if (el.querySelector('i.vmi')) return;
    el.appendChild(document.createElement('i')).className = 'vmi';
  }
  function mountAll(root) {
    (root || document).querySelectorAll('.voco-mascot').forEach(mount);
  }

  // 状态切换辅助:VocoMascot.setState(el, 'listening')
  window.VocoMascot = {
    mountAll: mountAll,
    setState: function (el, state) {
      if (el) el.dataset.state = state;
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => mountAll());
  } else {
    mountAll();
  }
})();
