// Run with `node tests/session_status.cjs` to exercise the browser status UI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const element = () => {
  const classes = new Set();
  const listeners = {};
  return {
    classList: {
      toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
      contains: (name) => classes.has(name),
    },
    addEventListener(name, callback) { listeners[name] = callback; },
    listeners,
    dataset: {},
    disabled: false,
    textContent: '',
  };
};
const offline = element();
offline.dataset.statusUrl = '/session/status/';
const dot = element();
const label = element();
const notice = element();
notice.dataset.sessionExpiresAt = new Date(Date.now() + 60 * 60 * 1000).toISOString();
const message = element();
const extend = element();
notice.querySelector = (selector) => ({
  '[data-session-message]': message,
  '[data-session-extend]': extend,
})[selector] || null;
const root = { setAttribute() {}, removeAttribute() {} };
const windowListeners = {};
const document = {
  documentElement: root,
  hidden: false,
  querySelector: (selector) => ({
    '[data-offline-notice]': offline,
    '.status-dot': dot,
    '[data-connection-label]': label,
    '[data-session-expires-at]': notice,
    'form[action$="/logout/"] input[name="csrfmiddlewaretoken"]': { value: 'csrf' },
  })[selector] || null,
  querySelectorAll: () => [],
  addEventListener() {},
};
const navigator = { onLine: false };
let nextFetch;
const context = {
  document, navigator,
  window: {
    addEventListener: (name, callback) => { windowListeners[name] = callback; },
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  },
  localStorage: { getItem: () => null },
  AbortController,
  fetch: async () => {
    if (nextFetch instanceof Error) throw nextFetch;
    return nextFetch;
  },
};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'),
  context,
);
const json = (status, data) => ({
  status, ok: status === 200,
  headers: { get: () => 'application/json' },
  json: async () => data,
});
const tick = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  assert.equal(label.textContent, 'Browser offline');
  assert.equal(offline.classList.contains('is-shown'), true);

  navigator.onLine = true;
  nextFetch = new Error('server down');
  windowListeners.online();
  await tick();
  assert.equal(label.textContent, 'Server unavailable');
  assert.equal(offline.classList.contains('is-shown'), true);

  nextFetch = json(200, { expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString() });
  windowListeners.online();
  await tick();
  assert.equal(label.textContent, 'Server connected');
  assert.equal(offline.classList.contains('is-shown'), false);

  nextFetch = json(401, { authenticated: false });
  windowListeners.online();
  await tick();
  assert.equal(label.textContent, 'Session expired');
  assert.equal(notice.classList.contains('is-shown'), true);

  nextFetch = json(200, { expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString() });
  windowListeners.online();
  await tick();
  nextFetch = new Error('extension failed');
  await extend.listeners.click();
  assert.equal(notice.classList.contains('is-shown'), true);
  assert.match(message.textContent, /extension failed/i);
  assert.equal(label.textContent, 'Server unavailable');
})().catch((error) => { console.error(error); process.exitCode = 1; });
