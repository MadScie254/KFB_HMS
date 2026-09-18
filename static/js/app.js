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
