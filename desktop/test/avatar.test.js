// The avatars drawn from names: the same name gives the same picture, and the picture keeps its symmetry.
const { test } = require('node:test');
const assert = require('node:assert');
const { svg, shapes } = require('../renderer/avatar');

const N = 8;
const key = (s) => (s.circle ? `c${s.circle.map((n) => n.toFixed(3)).join(',')}` : `p${s.points
  .map((p) => p.map((n) => n.toFixed(3)).join(',')).sort().join(';')}`) + s.color;
const picture = (drawn) => new Set(drawn.shapes.map(key));
const moved = (drawn, op) => new Set(drawn.shapes.map((s) => key(s.circle
  ? { ...s, circle: [...op(s.circle[0], s.circle[1]), s.circle[2]] }
  : { ...s, points: s.points.map(([x, y]) => op(x, y)) })));

test('a name always gives the same picture, and different names different ones', () => {
  assert.equal(svg('yuxiang'), svg('yuxiang'));
  assert.notEqual(svg('yuxiang'), svg('ann'));
  assert.notEqual(svg('yuxiang', { folds: 4 }), svg('yuxiang', { folds: 8 }));
  assert.match(svg('yuxiang'), /^<svg [^>]*viewBox="-1 -1 10 10"/);
});

test("a person's picture has 4-fold symmetry and the assistant's 8-fold", () => {
  const mirrorX = (x, y) => [N - x, y];
  const mirrorY = (x, y) => [x, N - y];
  const diagonal = (x, y) => [y, x];
  for (const name of ['yuxiang', 'ann', 'bob', 'continuum', '小林']) {
    const four = shapes(name, { folds: 4 });
    assert.ok(four.filled > 0);
    assert.deepEqual(moved(four, mirrorX), picture(four));
    assert.deepEqual(moved(four, mirrorY), picture(four));
    const eight = shapes(name, { folds: 8 });
    for (const op of [mirrorX, mirrorY, diagonal]) assert.deepEqual(moved(eight, op), picture(eight));
  }
});

test('the hues can be held to a range', () => {
  for (const name of ['a', 'b', 'c', 'd']) {
    const hue = Number(/hsl\((\d+)/.exec(shapes(name, { folds: 8, hues: [160, 200] }).background)[1]);
    assert.ok(hue >= 160 && hue <= 200, String(hue));
  }
});
