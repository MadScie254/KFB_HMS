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
    let dirty = false;
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('submit', () => { dirty = false; });
    window.addEventListener('beforeunload', (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });
  });

  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
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
    table.querySelectorAll('thead th').forEach((th, index) => {
      if (th.hasAttribute('data-no-sort')) return;
      th.setAttribute('data-sortable', '');
      th.setAttribute('tabindex', '0');
      th.setAttribute('role', 'button');
      const sort = () => {
        const current = th.getAttribute('data-sort');
        const direction = current === 'asc' ? 'desc' : 'asc';
        table.querySelectorAll('thead th').forEach((other) => other.removeAttribute('data-sort'));
        th.setAttribute('data-sort', direction);
        const rows = Array.from(body.querySelectorAll('tr'));
        rows.sort((a, b) => {
          const left = cellValue(a, index);
          const right = cellValue(b, index);
          if (left < right) return direction === 'asc' ? -1 : 1;
          if (left > right) return direction === 'asc' ? 1 : -1;
          return 0;
        });
        rows.forEach((row) => body.appendChild(row));
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

    const links = () => Array.from(list.querySelectorAll('a'));
    const highlight = (index) => {
      const items = links();
      if (!items.length) return;
      active = (index + items.length) % items.length;
      items.forEach((a, i) => a.classList.toggle('is-active', i === active));
      items[active].scrollIntoView({ block: 'nearest' });
    };

    const open = () => {
      lastFocus = document.activeElement;
      palette.classList.add('is-open');
      input.value = '';
      list.innerHTML = '<li class="palette-empty">Start typing a name, number or batch.</li>';
      input.focus();
    };
    const close = () => {
      palette.classList.remove('is-open');
      active = -1;
      input.blur();
      // Only return focus somewhere that can actually hold it; otherwise the
      // hidden search box keeps it and eats every following keystroke.
      if (lastFocus && lastFocus !== document.body && typeof lastFocus.focus === 'function') {
        lastFocus.focus();
      }
    };

    const render = (results) => {
      if (!results.length) {
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
      highlight(0);
    };

    const search = (term) => {
      if (term.trim().length < 2) {
        list.innerHTML = '<li class="palette-empty">Start typing a name, number or batch.</li>';
        return;
      }
      fetch(`${endpoint}?q=${encodeURIComponent(term)}`, { headers: { 'X-Requested-With': 'fetch' } })
        .then((response) => (response.ok ? response.json() : { results: [] }))
        .then((data) => render(data.results || []))
        .catch(() => { list.innerHTML = '<li class="palette-empty">Search is unavailable right now.</li>'; });
    };

    input.addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => search(input.value), 180);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); highlight(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); highlight(active - 1); }
      else if (event.key === 'Enter') {
        const items = links();
        if (items[active]) { event.preventDefault(); items[active].click(); }
      } else if (event.key === 'Escape') { close(); }
    });
    palette.addEventListener('click', (event) => { if (event.target === palette) close(); });
    document.querySelectorAll('[data-palette-open]').forEach((b) => b.addEventListener('click', open));

    /* ---- Keyboard shortcuts ---------------------------------------------- */
    const typing = (target) => target && (
      target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
      target.tagName === 'SELECT' || target.isContentEditable);

    let chord = null;
    let chordTimer = null;
    const go = { d: '/', p: '/patients/', q: '/queue/', s: '/stock/', b: '/brief/', r: '/reports/' };

    document.addEventListener('keydown', (event) => {
      if ((event.key === 'k' || event.key === 'K') && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        palette.classList.contains('is-open') ? close() : open();
        return;
      }
      if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === '/') { event.preventDefault(); open(); return; }
      if (event.key === '?') { event.preventDefault(); document.querySelector('[data-shortcut-help]')?.toggleAttribute('hidden'); return; }
      if (event.key === 'g') {
        chord = 'g';
        window.clearTimeout(chordTimer);
        chordTimer = window.setTimeout(() => { chord = null; }, 1200);
        return;
      }
      if (chord === 'g' && go[event.key]) {
        event.preventDefault();
        chord = null;
        window.location.assign(go[event.key]);
      }
    });
  }

  /* ---- Connection awareness ----------------------------------------------
     A ward on an unreliable link should learn the server is unreachable
     before somebody finishes typing a payment into a form that cannot post. */
  const offlineBar = document.querySelector('[data-offline-notice]');
  const dot = document.querySelector('.status-dot');
  const connectionLabel = document.querySelector('[data-connection-label]');
  const setOnline = (online) => {
    if (offlineBar) offlineBar.classList.toggle('is-shown', !online);
    if (dot) dot.classList.toggle('is-offline', !online);
    if (connectionLabel) connectionLabel.textContent = online ? 'Server connected' : 'No connection';
  };
  window.addEventListener('online', () => setOnline(true));
  window.addEventListener('offline', () => setOnline(false));
  if (!navigator.onLine) setOnline(false);

  /* ---- Session expiry ----------------------------------------------------
     Eight hours is long enough to start a clinical note and lose it. */
  const sessionNotice = document.querySelector('[data-session-notice]');
  if (sessionNotice) {
    const minutes = Number(sessionNotice.dataset.sessionNotice || '0');
    if (minutes > 0) {
      window.setTimeout(() => sessionNotice.classList.add('is-shown'), Math.max(minutes - 5, 1) * 60000);
      sessionNotice.querySelector('[data-session-extend]')?.addEventListener('click', () => {
        fetch(window.location.href, { method: 'HEAD', cache: 'no-store' })
          .finally(() => sessionNotice.classList.remove('is-shown'));
      });
    }
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
    form.addEventListener('submit', (event) => {
      if (form.dataset.confirmed === 'yes') return;
      event.preventDefault();
      if (window.confirm(form.dataset.confirm)) {
        form.dataset.confirmed = 'yes';
        form.requestSubmit(event.submitter || undefined);
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
