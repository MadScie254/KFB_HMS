(() => {
  const sidebar = document.querySelector('.sidebar');
  const menuButtons = document.querySelectorAll('[data-menu-toggle]');
  if (menuButtons.length && sidebar) {
    const setMenu = (open) => {
      sidebar.classList.toggle('open', open);
      menuButtons.forEach((button) => button.setAttribute('aria-expanded', String(open)));
    };
    menuButtons.forEach((button) => button.addEventListener('click', () => setMenu(!sidebar.classList.contains('open'))));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') setMenu(false);
    });
  }

  document.querySelectorAll('[data-formset]').forEach((formset) => {
    const list = formset.querySelector('[data-form-list]');
    const template = formset.querySelector('[data-form-template]');
    const total = formset.querySelector('input[name$="-TOTAL_FORMS"]');
    const add = formset.querySelector('[data-add-form]');
    if (!list || !template || !total || !add) return;

    const wireRemove = (row) => {
      const remove = row.querySelector('[data-remove-form]');
      if (!remove) return;
      remove.addEventListener('click', () => {
        const deletion = row.querySelector('input[name$="-DELETE"]');
        const filled = Array.from(row.querySelectorAll('input:not([type="hidden"]), select, textarea'))
          .some((field) => field.value);
        if (deletion && filled) {
          deletion.checked = true;
          row.classList.add('is-removed');
        } else {
          row.remove();
        }
      });
    };
    list.querySelectorAll('[data-form-row]').forEach(wireRemove);
    add.addEventListener('click', () => {
      const index = Number(total.value);
      const wrapper = document.createElement('div');
      wrapper.innerHTML = template.innerHTML.replaceAll('__prefix__', String(index)).trim();
      const row = wrapper.firstElementChild;
      list.appendChild(row);
      total.value = String(index + 1);
      wireRemove(row);
      row.querySelector('select, input, textarea')?.focus();
    });
  });

  document.querySelectorAll('[data-unsaved-warning]').forEach((form) => {
    // A rejected POST returns the user's entered values with field errors.
    // Keep the warning active until those values are successfully submitted.
    let dirty = Boolean(form.querySelector('.field-error, .message.error'));
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('submit', (event) => {
      if (!event.defaultPrevented) dirty = false;
    });
    window.addEventListener('beforeunload', (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });
  });

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (event.defaultPrevented) return;
      const button = event.submitter || form.querySelector('button[type="submit"]');
      // Defer disabling until after the browser has captured the submitter's
      // name/value (for example action=sign or decision=approve).
      window.setTimeout(() => {
        if (!button) return;
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = 'Saving...';
      }, 0);
    });
  });
})();

/* ============================================================================
   Interaction layer added 20 September 2026.
   Everything here is an enhancement: with JavaScript unavailable the pages
   still render, submit and navigate exactly as before.
   ========================================================================== */
(() => {
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem(key); return v === null ? fallback : v; }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  /* ---- Theme -------------------------------------------------------------
     Follows the operating system until somebody chooses, then remembers. */
  const root = document.documentElement;
  const applyTheme = (theme) => {
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      const dark = theme === 'dark' || (theme === 'system' &&
        window.matchMedia('(prefers-color-scheme: dark)').matches);
      button.setAttribute('aria-pressed', String(dark));
      button.setAttribute('title', dark ? 'Switch to the light theme' : 'Switch to the dark theme');
    });
  };
  applyTheme(store.get('kfb-theme', 'system'));
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const dark = root.getAttribute('data-theme') === 'dark' ||
        (!root.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
      const next = dark ? 'light' : 'dark';
      store.set('kfb-theme', next);
      applyTheme(next);
    });
  });

  /* ---- Row density ------------------------------------------------------- */
  const applyDensity = (mode) => {
    if (mode === 'compact') root.setAttribute('data-density', 'compact');
    else root.removeAttribute('data-density');
    document.querySelectorAll('[data-density-toggle]').forEach((b) =>
      b.setAttribute('aria-pressed', String(mode === 'compact')));
  };
  applyDensity(store.get('kfb-density', 'comfortable'));
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-density-toggle]');
    if (!button) return;
    const next = root.getAttribute('data-density') === 'compact' ? 'comfortable' : 'compact';
    store.set('kfb-density', next);
    applyDensity(next);
  });

  /* ---- Tables that stack into cards on a phone ---------------------------
     Each cell is labelled from its column header, so the CSS can drop the
     header row and still say what every value is. Without this the columns
     that matter most sit off-screen behind an undiscoverable sideways swipe. */
  document.querySelectorAll('.table-wrap table').forEach((table) => {
    const headers = Array.from(table.querySelectorAll('thead th')).map((th) => th.textContent.trim());
    if (!headers.length) return;
    table.querySelectorAll('tbody tr').forEach((row) => {
      Array.from(row.children).forEach((cell, index) => {
        if (cell.colSpan > 1) return;
        if (headers[index]) cell.setAttribute('data-label', headers[index]);
      });
    });
    table.closest('.table-wrap').classList.add('stacked');
  });

  /* ---- Sortable columns --------------------------------------------------
     Client side and on the rows already present. Paginated pages say so, so
     nobody mistakes a sorted page for a sorted dataset. */
  const cellValue = (row, index) => {
    const cell = row.children[index];
    if (!cell) return '';
    const raw = (cell.getAttribute('data-sort-value') ?? cell.textContent).trim();
    const numeric = raw.replace(/[^0-9.-]/g, '');
    if (numeric && /\d/.test(numeric) && /^[^A-Za-z]*$/.test(raw.replace(/KES|,/g, '').trim())) {
      const parsed = Number.parseFloat(numeric);
      if (!Number.isNaN(parsed)) return parsed;
    }
    return raw.toLowerCase();
  };

  document.querySelectorAll('table[data-sortable-table]').forEach((table) => {
    const body = table.querySelector('tbody');
    if (!body) return;
    const grouped = table.hasAttribute('data-sort-grouped');
    table.querySelectorAll('thead th').forEach((th, index) => {
      if (th.hasAttribute('data-no-sort')) return;
      th.setAttribute('data-sortable', '');
      th.setAttribute('tabindex', '0');
      const sort = () => {
        const current = th.getAttribute('data-sort');
        const direction = current === 'asc' ? 'desc' : 'asc';
        table.querySelectorAll('thead th').forEach((other) => {
          other.removeAttribute('data-sort');
          other.removeAttribute('aria-sort');
        });
        th.setAttribute('data-sort', direction);
        const rows = grouped ? Array.from(table.tBodies) : Array.from(body.rows);
        rows.sort((a, b) => {
          const left = cellValue(grouped ? a.rows[0] : a, index);
          const right = cellValue(grouped ? b.rows[0] : b, index);
          if (left === '' && right !== '') return 1;
          if (right === '' && left !== '') return -1;
          if (left < right) return direction === 'asc' ? -1 : 1;
          if (left > right) return direction === 'asc' ? 1 : -1;
          return 0;
        });
        rows.forEach((row) => (grouped ? table : body).appendChild(row));
        th.setAttribute('aria-sort', direction === 'asc' ? 'ascending' : 'descending');
      };
      th.addEventListener('click', sort);
      th.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); sort(); }
      });
    });
  });

  /* ---- Command palette ---------------------------------------------------- */
  const palette = document.querySelector('[data-palette]');
  if (palette) {
    const input = palette.querySelector('input');
    const list = palette.querySelector('.palette-results');
    const endpoint = palette.dataset.palette;
    let active = -1;
    let timer = null;
    let lastFocus = null;
    let searchController = null;
    let searchSequence = 0;

    const cancelSearch = () => {
      searchSequence += 1;
      if (searchController) searchController.abort();
      searchController = null;
    };

    const links = () => Array.from(list.querySelectorAll('a'));
    const highlight = (index) => {
      const items = links();
      if (!items.length) return;
      active = (index + items.length) % items.length;
      items.forEach((a, i) => a.classList.toggle('is-active', i === active));
      items[active].scrollIntoView({ block: 'nearest' });
    };

    const open = () => {
      cancelSearch();
      lastFocus = document.activeElement;
      palette.classList.add('is-open');
      input.value = '';
      list.innerHTML = '<li class="palette-empty">Start typing a name, number or batch.</li>';
      input.focus();
    };
    const close = () => {
      window.clearTimeout(timer);
      cancelSearch();
      palette.classList.remove('is-open');
      active = -1;
      input.blur();
      // Only return focus somewhere that can actually hold it; otherwise the
      // hidden search box keeps it and eats every following keystroke.
      if (lastFocus && lastFocus !== document.body && lastFocus.isConnected !== false &&
          typeof lastFocus.focus === 'function') {
        lastFocus.focus();
      }
      lastFocus = null;
    };

    const render = (results) => {
      if (!results.length) {
        active = -1;
        list.innerHTML = '<li class="palette-empty">Nothing matched, within what your role can open.</li>';
        return;
      }
      // Nothing from the server is interpolated into markup. Every value is
      // set as text or as a property, so a batch number containing a quote is
      // a batch number and a patient called <script> is a name.
      list.replaceChildren(...results.map((r) => {
        const item = document.createElement('li');
        const link = document.createElement('a');
        link.setAttribute('href', r.url);
        const dot = document.createElement('span');
        dot.className = 'kind-dot';
        dot.setAttribute('aria-hidden', 'true');
        dot.textContent = '\u2022';
        const body = document.createElement('span');
        const label = document.createElement('strong');
        label.textContent = r.label || '';
        const detail = document.createElement('small');
        detail.textContent = r.detail || '';
        body.append(label, detail);
        const kind = document.createElement('span');
        kind.className = 'kind';
        kind.textContent = r.kind || '';
        link.append(dot, body, kind);
        item.append(link);
        return item;
      }));
      // Pre-select the first hit: Enter should open the obvious answer without
      // making somebody press Down first.
      highlight(0);
    };

    const search = (term) => {
      if (term.trim().length < 2) {
        cancelSearch();
        active = -1;
        list.innerHTML = '<li class="palette-empty">Start typing a name, number or batch.</li>';
        return;
      }
      cancelSearch();
      const sequence = searchSequence;
      searchController = new AbortController();
      fetch(`${endpoint}?q=${encodeURIComponent(term)}`, {
        headers: { 'X-Requested-With': 'fetch' }, signal: searchController.signal,
      })
        .then((response) => (response.ok ? response.json() : { results: [] }))
        .then((data) => { if (sequence === searchSequence) render(data.results || []); })
        .catch((error) => {
          if (sequence === searchSequence && error.name !== 'AbortError') {
            active = -1;
            list.innerHTML = '<li class="palette-empty">Search is unavailable right now.</li>';
          }
        });
    };

    input.addEventListener('input', () => {
      window.clearTimeout(timer);
      cancelSearch();
      timer = window.setTimeout(() => search(input.value), 180);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); highlight(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); highlight(active - 1); }
      else if (event.key === 'Enter') {
        const items = links();
        if (items[active]) { event.preventDefault(); items[active].click(); }
      }
    });
    palette.addEventListener('click', (event) => { if (event.target === palette) close(); });
    document.querySelectorAll('[data-palette-open]').forEach((b) => b.addEventListener('click', open));

    /* ---- Keyboard shortcuts ---------------------------------------------- */
    const typing = (target) => target && (
      target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
      target.tagName === 'SELECT' || target.isContentEditable);

    let chord = null;
    let chordTimer = null;

    document.addEventListener('keydown', (event) => {
      if ((event.key === 'k' || event.key === 'K') && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        palette.classList.contains('is-open') ? close() : open();
        return;
      }
      if (palette.classList.contains('is-open')) {
        if (event.key === 'Escape') {
          event.preventDefault();
          close();
        } else if (event.key === 'Tab') {
          const items = [input, ...links()];
          const first = items[0];
          const last = items[items.length - 1];
          if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) {
            event.preventDefault();
            first.focus();
          }
        }
        return;
      }
      if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === '/') { event.preventDefault(); open(); return; }
      if (event.key === 'g') {
        chord = 'g';
        window.clearTimeout(chordTimer);
        chordTimer = window.setTimeout(() => { chord = null; }, 1200);
        return;
      }
      const destination = palette.dataset[`shortcut${event.key.toUpperCase()}`];
      if (chord === 'g' && destination) {
        event.preventDefault();
        chord = null;
        window.location.assign(destination);
      }
    });
  }

  /* ---- Verified server connection and session expiry -------------------- */
  const offlineBar = document.querySelector('[data-offline-notice]');
  const dot = document.querySelector('.status-dot');
  const connectionLabel = document.querySelector('[data-connection-label]');
  const sessionNotice = document.querySelector('[data-session-expires-at]');
  const sessionMessage = sessionNotice?.querySelector('[data-session-message]');
  const extendButton = sessionNotice?.querySelector('[data-session-extend]');
  const statusUrl = offlineBar?.dataset.statusUrl;
  let expiresAt = Date.parse(sessionNotice?.dataset.sessionExpiresAt || '');
  let extensionFailed = false;
  let latestCheck = 0;
  const setConnection = (state) => {
    const unavailable = state === 'offline' || state === 'unreachable';
    offlineBar?.classList.toggle('is-shown', unavailable);
    dot?.classList.toggle('is-offline', unavailable);
    if (connectionLabel) connectionLabel.textContent = {
      connected: 'Server connected', offline: 'Browser offline',
      unreachable: 'Server unavailable', expired: 'Session expired',
    }[state] || 'Checking server';
  };
  const refreshSessionWarning = () => {
    if (!sessionNotice || !Number.isFinite(expiresAt)) return;
    const remaining = expiresAt - Date.now();
    const expired = remaining <= 0;
    const show = expired || remaining <= 5 * 60 * 1000 || extensionFailed;
    sessionNotice.classList.toggle('is-shown', show);
    if (sessionMessage) sessionMessage.textContent = extensionFailed
      ? 'Session extension failed. Save your work and try again.'
      : expired ? 'Your session has expired. Sign in again before saving.'
        : 'Your session is about to end. Save your work.';
    if (extendButton) extendButton.disabled = expired;
  };
  const updateExpiry = (value) => {
    const parsed = Date.parse(value || '');
    if (Number.isFinite(parsed)) expiresAt = parsed;
    refreshSessionWarning();
  };
  const checkServer = async () => {
    if (!statusUrl) return;
    if (!navigator.onLine) { setConnection('offline'); return; }
    const check = ++latestCheck;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(statusUrl, {
        credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      if (check !== latestCheck) return;
      if (response.status === 401) {
        setConnection('expired');
        expiresAt = Date.now();
        refreshSessionWarning();
        return;
      }
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Server status unavailable');
      }
      const data = await response.json();
      if (check !== latestCheck) return;
      setConnection('connected');
      updateExpiry(data.expires_at);
    } catch {
      if (check === latestCheck) setConnection(navigator.onLine ? 'unreachable' : 'offline');
    } finally {
      window.clearTimeout(timeout);
    }
  };
  if (statusUrl) {
    checkServer();
    window.addEventListener('online', checkServer);
    window.addEventListener('offline', () => { latestCheck += 1; setConnection('offline'); });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) checkServer();
    });
    window.setInterval(() => { if (!document.hidden) checkServer(); }, 60000);
  }
  if (sessionNotice) {
    refreshSessionWarning();
    window.setInterval(refreshSessionWarning, 30000);
    extendButton?.addEventListener('click', async () => {
      const csrf = document.querySelector('form[action$="/logout/"] input[name="csrfmiddlewaretoken"]')?.value;
      extendButton.disabled = true;
      try {
        if (!csrf) throw new Error('Missing CSRF token');
        const response = await fetch(statusUrl, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: { 'X-CSRFToken': csrf, Accept: 'application/json' },
        });
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
          throw new Error('Session extension rejected');
        }
        const data = await response.json();
        if (!data.expires_at) throw new Error('Missing session expiry');
        extensionFailed = false;
        updateExpiry(data.expires_at);
        setConnection('connected');
      } catch {
        extensionFailed = true;
        refreshSessionWarning();
        setConnection(navigator.onLine ? 'unreachable' : 'offline');
      } finally {
        extendButton.disabled = expiresAt <= Date.now();
      }
    });
  }

  /* ---- Dismissible messages ---------------------------------------------- */
  document.querySelectorAll('.messages .message').forEach((message) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'message-dismiss';
    button.setAttribute('aria-label', 'Dismiss this message');
    button.textContent = '×';
    button.addEventListener('click', () => message.remove());
    message.appendChild(button);
  });

  /* ---- Confirmation for what cannot be undone ---------------------------- */
  document.querySelectorAll('[data-confirm]').forEach((form) => {
    let replayingConfirmedSubmit = false;
    form.addEventListener('submit', (event) => {
      if (replayingConfirmedSubmit) return;
      event.preventDefault();
      if (window.confirm(form.dataset.confirm)) {
        replayingConfirmedSubmit = true;
        try { form.requestSubmit(event.submitter || undefined); }
        finally { replayingConfirmedSubmit = false; }
      }
    }, true);
  });

  /* ---- Back to top on the long ledger pages ------------------------------ */
  const toTop = document.querySelector('[data-to-top]');
  if (toTop) {
    const reveal = () => toTop.classList.toggle('is-shown', window.scrollY > 900);
    window.addEventListener('scroll', reveal, { passive: true });
    toTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    reveal();
  }
})();
