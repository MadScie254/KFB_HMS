// Run with `node tests/unsaved_warning.cjs` to check interrupted form entry.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const formListeners = {};
const windowListeners = {};
const form = {
  addEventListener(event, callback) { (formListeners[event] ||= []).push(callback); },
  querySelector(selector) { return selector === '.field-error, .message.error' ? {} : null; },
};
const document = {
  documentElement: { setAttribute() {}, removeAttribute() {} },
  querySelector: () => null,
  querySelectorAll(selector) { return selector === '[data-unsaved-warning]' || selector === 'form' ? [form] : []; },
  addEventListener() {},
};
const window = {
  addEventListener(event, callback) { windowListeners[event] = callback; },
  setTimeout() {},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'app.js'), 'utf8'), {
  document, window,
  navigator: { onLine: true },
  localStorage: { getItem: () => null },
});

const warned = () => {
  let prevented = false;
  windowListeners.beforeunload({ preventDefault() { prevented = true; }, returnValue: null });
  return prevented;
};
const dispatch = (name, event = {}) => (formListeners[name] || []).forEach((listener) => listener(event));
assert.equal(warned(), true, 'Returned validation errors keep entered values protected');
dispatch('submit', { defaultPrevented: false, submitter: null });
assert.equal(warned(), false, 'Successful submission clears the warning');
dispatch('input');
assert.equal(warned(), true, 'Editing starts the warning again');
dispatch('submit', { defaultPrevented: true });
assert.equal(warned(), true, 'Cancelled submission still protects entered values');
