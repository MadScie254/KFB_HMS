// Run with `node tests/palette_search.cjs` to exercise out-of-order searches.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = {};
const element = () => ({
  children: [],
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  setAttribute() {},
  addEventListener(event, handler) { this.listeners[event] = handler; },
  listeners: {},
  append(...children) { this.children.push(...children); },
  scrollIntoView() {},
  focus() {},
  blur() {},
  textContent: '',
});
const input = element();
const list = element();
list.querySelectorAll = (selector) => selector === 'a' ? list.children.map((item) => item.children[0]) : [];
list.replaceChildren = (...items) => { list.children = items; };
Object.defineProperty(list, 'innerHTML', { set: () => { list.children = []; } });
const palette = element();
palette.dataset = { palette: '/search/' };
palette.querySelector = (selector) => ({ input, '.palette-results': list })[selector] || null;
const opener = element();
const document = {
  documentElement: { setAttribute() {}, removeAttribute() {} },
  activeElement: null,
  createElement: () => element(),
  querySelector: (selector) => selector === '[data-palette]' ? palette : null,
  querySelectorAll: (selector) => selector === '[data-palette-open]' ? [opener] : [],
  addEventListener(event, handler) { listeners[event] = handler; },
};
const timers = new Map();
let timerId = 0;
const requests = [];
const window = {
  addEventListener() {},
  setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
  clearTimeout(id) { timers.delete(id); },
};
const fetch = (url, options) => new Promise((resolve) => requests.push({ url, options, resolve }));
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'), {
  document, window, fetch, AbortController,
  navigator: { onLine: true },
  localStorage: { getItem: () => null },
});

const flushTimer = () => {
  const [id, callback] = timers.entries().next().value;
  timers.delete(id);
  callback();
};
const flushPromises = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  opener.listeners.click();
  input.value = 'al';
  input.listeners.input();
  flushTimer();
  input.value = 'be';
  input.listeners.input();
  assert.equal(requests[0].options.signal.aborted, true);
  flushTimer();
  assert.equal(requests.length, 2);

  requests[1].resolve({ ok: true, json: async () => ({
    results: [{ label: 'Beta', detail: 'New result', kind: 'Patient', url: '/patients/beta/' }],
  }) });
  await flushPromises();
  requests[0].resolve({ ok: true, json: async () => ({
    results: [{ label: 'Alpha', detail: 'Old result', kind: 'Patient', url: '/patients/alpha/' }],
  }) });
  await flushPromises();
  assert.equal(list.children.length, 1);
  assert.equal(list.children[0].children[0].children[1].children[0].textContent, 'Beta');
})().catch((error) => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
