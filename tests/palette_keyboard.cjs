// Run with `node tests/palette_keyboard.cjs` to check modal keyboard behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const listeners = {};
const document = {
  documentElement: { setAttribute() {}, removeAttribute() {} },
  activeElement: null,
  querySelector: (selector) => selector === '[data-palette]' ? palette : null,
  querySelectorAll: (selector) => selector === '[data-palette-open]' ? [opener] : [],
  addEventListener(event, handler) { listeners[event] = handler; },
};
const element = () => {
  const classes = new Set();
  return {
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
    },
    listeners: {},
    addEventListener(event, handler) { this.listeners[event] = handler; },
    focus() { document.activeElement = this; },
    blur() { if (document.activeElement === this) document.activeElement = null; },
    setAttribute() {},
  };
};
const input = element();
const list = element();
list.links = [];
list.querySelectorAll = (selector) => selector === 'a' ? list.links : [];
Object.defineProperty(list, 'innerHTML', { set() { list.links = []; } });
const palette = element();
palette.dataset = { palette: '/search/' };
palette.querySelector = (selector) => ({ input, '.palette-results': list })[selector] || null;
const opener = element();
const outside = element();

vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'), {
  document,
  window: { addEventListener() {}, clearTimeout() {} },
  navigator: { onLine: true },
  localStorage: { getItem: () => null },
});

const key = (value, target, shiftKey = false) => {
  let prevented = false;
  listeners.keydown({ key: value, target, shiftKey, preventDefault() { prevented = true; } });
  return prevented;
};

opener.focus();
opener.listeners.click();
assert.equal(palette.classList.contains('is-open'), true);
assert.equal(document.activeElement, input);

const firstLink = element();
const lastLink = element();
list.links = [firstLink, lastLink];
assert.equal(key('Tab', input, true), true);
assert.equal(document.activeElement, lastLink);
assert.equal(key('Tab', lastLink), true);
assert.equal(document.activeElement, input);

outside.focus();
assert.equal(key('Tab', outside), true);
assert.equal(document.activeElement, input);
assert.equal(key('Tab', input), false, 'The browser should move to the first result normally');

firstLink.focus();
assert.equal(key('Escape', firstLink), true);
assert.equal(palette.classList.contains('is-open'), false);
assert.equal(document.activeElement, opener, 'Focus returns to the control that opened search');
assert.equal(key('?', opener), false, 'Removed help shortcut does not swallow typing');
