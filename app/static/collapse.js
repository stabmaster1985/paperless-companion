// Kästen (.card mit Überschrift) per Klick auf die Überschrift auf- und zuklappen.
// Der Zustand wird je Seite und Überschrift im Browser gemerkt (localStorage).
(() => {
  const page = location.pathname.replace(/\/\d+(?=\/|$)/g, '/:id');
  const keyOf = (h) => 'card:' + page + ':' + h.textContent.replace(/\(\d+[^)]*\)|\d+/g, '').replace(/[?\s]+/g, ' ').trim();
  const read = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const write = (k, v) => { try { v ? localStorage.setItem(k, '1') : localStorage.removeItem(k); } catch (e) { /* privat */ } };

  document.querySelectorAll('.card').forEach((card) => {
    const h = card.querySelector(':scope > h2, :scope > h3');
    if (!h || card.classList.contains('no-collapse')) return;
    const key = keyOf(h);
    card.classList.add('collapsible');
    h.setAttribute('role', 'button');
    h.setAttribute('tabindex', '0');
    h.title = 'Klicken zum Auf- oder Zuklappen';
    const set = (collapsed) => {
      card.classList.toggle('collapsed', collapsed);
      h.setAttribute('aria-expanded', String(!collapsed));
    };
    set(read(key) === '1');
    const toggle = (e) => {
      if (e.target.closest('.help, a, button, input, select')) return; // Tooltips und Links nicht abfangen
      const collapsed = !card.classList.contains('collapsed');
      set(collapsed);
      write(key, collapsed);
    };
    h.addEventListener('click', toggle);
    h.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); } });
  });

  // Alle auf-/zuklappen (Knopf in der Seitenhilfe)
  document.querySelectorAll('[data-collapse-all]').forEach((btn) => btn.addEventListener('click', () => {
    const collapse = btn.dataset.collapseAll === '1';
    document.querySelectorAll('.card.collapsible').forEach((card) => {
      const h = card.querySelector(':scope > h2, :scope > h3');
      card.classList.toggle('collapsed', collapse);
      h.setAttribute('aria-expanded', String(!collapse));
      write(keyOf(h), collapse);
    });
  }));
})();

// ?-Tooltips auch per Antippen/Klick öffnen (auf Handys gibt es kein Darüberfahren); Klick daneben schließt sie
document.addEventListener('click', (e) => {
  const tip = e.target.closest('.help');
  document.querySelectorAll('.help.show').forEach((t) => { if (t !== tip) t.classList.remove('show'); });
  if (tip) { e.preventDefault(); e.stopPropagation(); tip.classList.toggle('show'); }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.help.show').forEach((t) => t.classList.remove('show'));
});
