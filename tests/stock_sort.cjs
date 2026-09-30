// Run with `node tests/stock_sort.cjs`. Exercise the real table handler with
// product groups that each contain more than one batch-detail row.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const product = (name, batches) => ({
  rows: [
    { children: [{ textContent: name, getAttribute: () => null }] },
    ...batches.map((batch) => ({ batch })),
  ],
});
const zeta = product('Zeta tablets', ['Z-1', 'Z-2']);
const alpha = product('Alpha tablets', ['A-1', 'A-2']);
const groups = [zeta, alpha];
const attributes = new Map();
let click;
const heading = {
  hasAttribute: () => false,
  setAttribute: (key, value) => attributes.set(key, value),
  getAttribute: (key) => attributes.get(key),
  removeAttribute: (key) => attributes.delete(key),
  addEventListener: (event, callback) => { if (event === 'click') click = callback; },
};
const table = {
  get tBodies() { return groups; },
  hasAttribute: (key) => key === 'data-sort-grouped',
  querySelector: (selector) => selector === 'tbody' ? groups[0] : null,
  querySelectorAll: (selector) => selector === 'thead th' ? [heading] : [],
  appendChild(group) {
    groups.splice(groups.indexOf(group), 1);
    groups.push(group);
  },
};
const root = { setAttribute() {}, removeAttribute() {} };
const document = {
  documentElement: root,
  querySelector: () => null,
  querySelectorAll: (selector) => selector === 'table[data-sortable-table]' ? [table] : [],
  addEventListener() {},
};
const context = {
  document,
  window: { addEventListener() {} },
  navigator: { onLine: true },
  localStorage: { getItem: () => null },
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'),
  context,
);
assert.equal(typeof click, 'function');

click();
assert.deepEqual(groups, [alpha, zeta]);
assert.deepEqual(groups.map((group) => group.rows.slice(1).map((row) => row.batch)), [
  ['A-1', 'A-2'], ['Z-1', 'Z-2'],
]);
assert.equal(attributes.get('aria-sort'), 'ascending');

click();
assert.deepEqual(groups, [zeta, alpha]);
assert.deepEqual(groups.map((group) => group.rows.slice(1).map((row) => row.batch)), [
  ['Z-1', 'Z-2'], ['A-1', 'A-2'],
]);
assert.equal(attributes.get('aria-sort'), 'descending');
