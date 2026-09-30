// Run with `node tests/sort_dates.cjs` to check chronological browser sorting.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const row = (label, date) => ({
  label,
  children: [{
    textContent: label,
    getAttribute: (key) => key === 'data-sort-value'
      ? (date ? String(Date.parse(date) / 1000) : '') : null,
  }],
});
const november = row('30 Nov 2026', '2026-11-30');
const december = row('1 Dec 2026', '2026-12-01');
const january = row('5 Jan 2027', '2027-01-05');
const unknown = row('Not recorded', null);
const rows = [january, unknown, december, november];
const body = {
  rows,
  appendChild(item) {
    rows.splice(rows.indexOf(item), 1);
    rows.push(item);
  },
};
let click;
const heading = {
  hasAttribute: () => false,
  setAttribute() {},
  getAttribute: () => null,
  removeAttribute() {},
  addEventListener: (event, callback) => { if (event === 'click') click = callback; },
};
const table = {
  querySelector: (selector) => selector === 'tbody' ? body : null,
  querySelectorAll: (selector) => selector === 'thead th' ? [heading] : [],
  hasAttribute: () => false,
};
const document = {
  documentElement: { setAttribute() {}, removeAttribute() {} },
  querySelector: () => null,
  querySelectorAll: (selector) => selector === 'table[data-sortable-table]' ? [table] : [],
  addEventListener() {},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'), {
  document,
  window: { addEventListener() {} },
  navigator: { onLine: true },
  localStorage: { getItem: () => null },
});
assert.equal(typeof click, 'function');
click();
assert.deepEqual(rows.map((item) => item.label), [
  '30 Nov 2026', '1 Dec 2026', '5 Jan 2027', 'Not recorded',
]);
