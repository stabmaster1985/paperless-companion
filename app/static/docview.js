// Dokumentvorschau: PDF-Seiten mit pdf.js in Fensterbreite zeichnen (kein Quer-Scrollen am Handy),
// Vergrößern/Verkleinern und „Groß“ (Vollbild). Bilder werden direkt gezeigt, anderes im iframe.
(() => {
  const base = document.currentScript.src.replace(/docview\.js.*$/, 'pdfjs/');
  const ZOOMS = [0.75, 1, 1.25, 1.5, 2, 3];
  const MAX_PAGES = 30;
  let pdfjs = null;

  function loadPdfjs() {
    if (pdfjs) return pdfjs;
    pdfjs = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = base + 'pdf.min.js';
      s.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = base + 'pdf.worker.min.js'; resolve(window.pdfjsLib); };
      s.onerror = reject;
      document.head.appendChild(s);
    });
    return pdfjs;
  }

  async function draw(view) {
    const pages = view.querySelector('.docview-pages');
    const pdf = view._pdf;
    const width = Math.max(200, pages.clientWidth - 16);
    const zoom = ZOOMS[view._zoom];
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const token = (view._token = (view._token || 0) + 1);
    const out = document.createElement('div');
    out.className = 'docview-canvas';
    pages.replaceChildren(out);
    for (let i = 1; i <= Math.min(pdf.numPages, MAX_PAGES); i++) {
      const page = await pdf.getPage(i);
      if (token !== view._token) return; // inzwischen neu gezeichnet (Zoom/Größe geändert)
      const scale = (width * zoom) / page.getViewport({ scale: 1 }).width;
      const vp = page.getViewport({ scale: scale * dpr });
      const canvas = document.createElement('canvas');
      canvas.width = vp.width; canvas.height = vp.height;
      canvas.style.width = (vp.width / dpr) + 'px';
      out.appendChild(canvas);
      await page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
    }
    if (pdf.numPages > MAX_PAGES) {
      out.insertAdjacentHTML('beforeend', `<p class="docview-more">Weitere ${pdf.numPages - MAX_PAGES} Seiten im neuen Tab.</p>`);
    }
  }

  async function load(view) {
    if (view._loaded) return;
    view._loaded = true;
    const pages = view.querySelector('.docview-pages');
    try {
      const resp = await fetch(view.dataset.src);
      if (!resp.ok) throw new Error(await resp.text());
      const type = resp.headers.get('content-type') || '';
      const data = await resp.arrayBuffer();
      if (type.includes('pdf')) {
        const lib = await loadPdfjs();
        view._pdf = await lib.getDocument({ data }).promise;
        await draw(view);
      } else if (type.startsWith('image/')) {
        view.classList.add('is-image');
        const img = document.createElement('img');
        img.className = 'docview-img';
        img.src = URL.createObjectURL(new Blob([data], { type }));
        pages.replaceChildren(img);
      } else {
        throw new Error('kein PDF');
      }
    } catch (err) {
      // Rückfall: Browser-eigene Anzeige
      const frame = document.createElement('iframe');
      frame.src = view.dataset.src + '#view=FitH';
      frame.title = 'Vorschau';
      pages.replaceChildren(frame);
    }
  }

  function setZoom(view, step) {
    view._zoom = Math.max(0, Math.min(ZOOMS.length - 1, view._zoom + step));
    view.querySelector('[data-act=zoom-label]').textContent = Math.round(ZOOMS[view._zoom] * 100) + ' %';
    if (view._pdf) draw(view);
    else view.style.setProperty('--img-zoom', ZOOMS[view._zoom]);
  }

  function toggleFull(view, on) {
    const full = on ?? !view.classList.contains('full');
    view.classList.toggle('full', full);
    document.body.classList.toggle('docview-open', full);
    view.querySelector('[data-act=full]').textContent = full ? '✕ Schließen' : '⤢ Groß';
    if (view._pdf) draw(view);
  }

  function init(view) {
    if (view._init) return;
    view._init = true;
    view._zoom = ZOOMS.indexOf(1);
    view.querySelector('[data-act=zoom-in]').addEventListener('click', () => setZoom(view, 1));
    view.querySelector('[data-act=zoom-out]').addEventListener('click', () => setZoom(view, -1));
    view.querySelector('[data-act=full]').addEventListener('click', () => toggleFull(view));
    let lastWidth = 0, timer;
    new ResizeObserver(() => {
      const w = view.querySelector('.docview-pages').clientWidth;
      if (!view._pdf || Math.abs(w - lastWidth) < 8) return;
      lastWidth = w;
      clearTimeout(timer);
      timer = setTimeout(() => draw(view), 150);
    }).observe(view.querySelector('.docview-pages'));
    if (!view.hasAttribute('data-lazy')) load(view);
  }

  document.addEventListener('keydown', (e) => {
    const open = document.querySelector('.docview.full');
    if (e.key === 'Escape' && open) { e.stopImmediatePropagation(); toggleFull(open, false); }
  }, true);

  window.docview = { init, load: (view) => { init(view); load(view); } };
  document.querySelectorAll('.docview').forEach(init);
})();
