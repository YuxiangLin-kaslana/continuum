// Avatars drawn from a name, for whoever has no picture of their own: a small kaleidoscope on an 8 x 8 grid, in the
// way of GitHub's identicons. A person's has 4-fold symmetry (mirrored left to right and top to bottom); the
// assistant's has 8-fold (mirrored across the diagonals too). The same name always gives the same picture.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ContinuumAvatar = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const N = 8;
  const HALF = N / 2;

  // A 32-bit hash of the name seeds a small PRNG (mulberry32).
  function seeded(text) {
    let h = 2166136261;
    for (const ch of String(text)) {
      h = Math.imul(h ^ ch.codePointAt(0), 16777619);
    }
    let a = h >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // The pieces a cell may hold, in the cell's own unit square.
  const PIECES = [
    { weight: 5, points: [[0, 0], [1, 0], [1, 1], [0, 1]] },
    { weight: 2, points: [[0, 0], [1, 0], [0, 1]] },
    { weight: 2, points: [[0, 0], [1, 0], [1, 1]] },
    { weight: 2, points: [[1, 0], [1, 1], [0, 1]] },
    { weight: 2, points: [[0, 0], [1, 1], [0, 1]] },
    { weight: 2, points: [[0.5, 0], [1, 0.5], [0.5, 1], [0, 0.5]] },
    { weight: 2, circle: 0.34 },
  ];
  const TOTAL = PIECES.reduce((sum, p) => sum + p.weight, 0);

  function pick(random, list, total) {
    let r = random() * total;
    for (const item of list) {
      r -= item.weight;
      if (r < 0) return item;
    }
    return list[list.length - 1];
  }

  // The symmetries: the mirrors through the middle, and for 8-fold the diagonal mirror as well.
  function symmetries(folds) {
    const mirrors = [(x, y) => [x, y], (x, y) => [N - x, y], (x, y) => [x, N - y], (x, y) => [N - x, N - y]];
    if (folds !== 8) return mirrors;
    return [...mirrors, ...mirrors.map((f) => (x, y) => f(y, x))];
  }

  // The cells drawn by chance: one quarter of the grid, or for 8-fold one eighth (the cells on or below the diagonal).
  function fundamental(folds) {
    const cells = [];
    for (let y = 0; y < HALF; y++) {
      for (let x = 0; x < HALF; x++) {
        if (folds === 8 && y > x) continue;
        cells.push([x, y]);
      }
    }
    return cells;
  }

  function palette(random, hues) {
    const [low, high] = hues || [0, 360];
    const h = low + random() * (high - low);
    const turn = 25 + random() * 30;
    return {
      background: `hsl(${h.toFixed(0)} 30% 9%)`,
      colors: [
        { weight: 5, value: `hsl(${h.toFixed(0)} 72% 62%)` },
        { weight: 3, value: `hsl(${((h + turn) % 360).toFixed(0)} 68% 50%)` },
        { weight: 2, value: `hsl(${((h - turn + 360) % 360).toFixed(0)} 60% 78%)` },
      ],
    };
  }

  // The picture as shapes on the grid, each already copied by every symmetry.
  function shapes(name, options = {}) {
    const folds = options.folds === 8 ? 8 : 4;
    const random = seeded(`${folds}:${name}`);
    const colors = palette(random, options.hues);
    const out = [];
    const ops = symmetries(folds);
    let filled = 0;
    for (const [x, y] of fundamental(folds)) {
      if (random() < 0.38) continue;
      filled++;
      const piece = pick(random, PIECES, TOTAL);
      const color = pick(random, colors.colors, 10).value;
      for (const op of ops) {
        if (piece.circle) {
          const [cx, cy] = op(x + 0.5, y + 0.5);
          out.push({ circle: [cx, cy, piece.circle], color });
        } else {
          out.push({ points: piece.points.map(([px, py]) => op(x + px, y + py)), color });
        }
      }
    }
    return { background: colors.background, shapes: out, filled };
  }

  const num = (n) => String(Math.round(n * 1000) / 1000);

  function svg(name, options = {}) {
    const drawn = shapes(name, options);
    const body = drawn.shapes.map((s) => (s.circle
      ? `<circle cx="${num(s.circle[0])}" cy="${num(s.circle[1])}" r="${num(s.circle[2])}" fill="${s.color}"/>`
      : `<polygon points="${s.points.map((p) => p.map(num).join(',')).join(' ')}" fill="${s.color}"/>`)).join('');
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 10 10" aria-hidden="true">`
      + `<rect x="-1" y="-1" width="10" height="10" fill="${drawn.background}"/>${body}</svg>`;
  }

  return { svg, shapes };
});
