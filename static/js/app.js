(() => {
  const menuButton = document.querySelector('[data-menu-toggle]');
  const sidebar = document.querySelector('.sidebar');
  if (menuButton && sidebar) {
    menuButton.addEventListener('click', () => sidebar.classList.toggle('open'));
  }

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
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]:focus') || form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = 'Saving…';
      }
    });
  });
})();

