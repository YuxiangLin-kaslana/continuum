// What an answer can show beyond words: highlighted code, formulas, diagrams, charts, pictures, and the files the
// assistant hands over. The heavy libraries load the first time something needs them; what they draw is kept, so a
// conversation redrawn does not draw it again.
(function () {
  const loading = new Map();
  function need(src) {
    if (!loading.has(src)) {
      loading.set(src, new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.onload = resolve;
        script.onerror = () => { loading.delete(src); reject(new Error(`加载不了 ${src}`)); };
        document.head.append(script);
      }));
    }
    return loading.get(src);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const failed = (node, what, error) => {
    node.classList.add('failed');
    node.replaceChildren(el('div', 'rich-error', `${what}没能画出来：${(error && error.message) || error}`),
      el('pre', '', node.dataset.src || ''));
  };

  // --- code ---

  function highlight(root) {
    if (!window.hljs) return;
    for (const code of root.querySelectorAll('pre code:not(.hljs)')) {
      const lang = code.dataset.lang;
      try {
        if (lang && window.hljs.getLanguage(lang)) {
          code.innerHTML = window.hljs.highlight(code.textContent, { language: lang, ignoreIllegals: true }).value;
        } else if (code.textContent.length < 20000) {
          code.innerHTML = window.hljs.highlightAuto(code.textContent).value;
        }
        code.classList.add('hljs');
      } catch (_) { /* plain is fine */ }
    }
  }

  // --- formulas: MathML, which the browser draws itself ---

  const mathDrawn = new Map();
  async function math(root) {
    const nodes = [...root.querySelectorAll('.math:not(.drawn)')];
    if (!nodes.length) return;
    await need('vendor/katex.min.js');
    for (const node of nodes) {
      const tex = node.dataset.tex;
      const block = node.classList.contains('block');
      const key = `${block}|${tex}`;
      if (!mathDrawn.has(key)) {
        mathDrawn.set(key, window.katex.renderToString(tex, { output: 'mathml', displayMode: block, throwOnError: false }));
      }
      node.innerHTML = mathDrawn.get(key);
      node.classList.add('drawn');
    }
  }

  // --- diagrams: Mermaid, drawn to a picture (its own styles stay inside it) ---

  const diagrams = new Map();
  let diagramCount = 0;
  async function diagram(node) {
    const src = node.dataset.src;
    if (!diagrams.has(src)) {
      diagrams.set(src, (async () => {
        await need('vendor/mermaid.min.js');
        if (!window.mermaid.__continuum) {
          window.mermaid.initialize({
            startOnLoad: false, securityLevel: 'strict', theme: 'base', darkMode: true, htmlLabels: false,
            flowchart: { htmlLabels: false }, fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif',
            themeVariables: { background: 'transparent', primaryColor: '#14302f', primaryTextColor: '#e8efed',
              primaryBorderColor: '#3aa9ac', lineColor: '#7d8b88', secondaryColor: '#1e2422', tertiaryColor: '#101413',
              mainBkg: '#14302f', nodeBorder: '#3aa9ac', clusterBkg: '#101413', textColor: '#e8efed', noteBkgColor: '#2a2214',
              noteTextColor: '#f2d9b0', actorBkg: '#14302f', actorBorder: '#3aa9ac', actorTextColor: '#e8efed', signalColor: '#b9c4c1',
              signalTextColor: '#e8efed', labelBoxBkgColor: '#14302f', fontSize: '14px' },
          });
          window.mermaid.__continuum = true;
        }
        const { svg } = await window.mermaid.render(`diagram-${++diagramCount}`, src);
        return `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(svg)))}`;
      })());
    }
    try {
      const url = await diagrams.get(src);
      const img = el('img', 'diagram-img');
      img.alt = '图';
      img.src = url;
      img.addEventListener('click', () => lightbox(url));
      node.replaceChildren(img);
    } catch (error) {
      diagrams.delete(src);
      failed(node, '图', error);
    }
  }

  // --- charts: a Chart.js configuration, in the window's colors ---

  const PALETTE = ['#3aa9ac', '#f0a23b', '#8b7cf6', '#e5677d', '#5fb878', '#4c8fd6', '#d4c05a', '#c77dd3'];
  function plain(value) {
    // Only data: a configuration holding functions or anything else would not be JSON.
    return JSON.parse(JSON.stringify(value));
  }
  async function chart(node) {
    let spec;
    try {
      spec = plain(JSON.parse(node.dataset.src));
    } catch (error) {
      failed(node, '图表', '不是合法的 JSON');
      return;
    }
    try {
      await need('vendor/chart.umd.min.js');
      const Chart = window.Chart;
      Chart.defaults.color = '#b9c4c1';
      Chart.defaults.borderColor = 'rgba(255,255,255,0.08)';
      Chart.defaults.font.family = 'Segoe UI, Microsoft YaHei, sans-serif';
      const round = ['pie', 'doughnut', 'polarArea'].includes(spec.type);
      (spec.data && spec.data.datasets || []).forEach((set, i) => {
        if (round) {
          set.backgroundColor = set.backgroundColor || (spec.data.labels || []).map((_, j) => PALETTE[j % PALETTE.length]);
          set.borderColor = set.borderColor || '#0b0d0c';
        } else {
          set.borderColor = set.borderColor || PALETTE[i % PALETTE.length];
          set.backgroundColor = set.backgroundColor || `${PALETTE[i % PALETTE.length]}${spec.type === 'line' ? '33' : 'cc'}`;
        }
      });
      spec.options = { responsive: true, maintainAspectRatio: false, animation: false, ...(spec.options || {}) };
      const canvas = el('canvas');
      const frame = el('div', 'chart-frame');
      frame.append(canvas);
      node.replaceChildren(frame);
      new Chart(canvas, spec);
    } catch (error) {
      failed(node, '图表', error);
    }
  }

  // Everything an answer holds beyond words, drawn once it is on the page.
  function enhance(root) {
    highlight(root);
    math(root).catch(() => {});
    for (const node of root.querySelectorAll('.diagram:not(.started)')) {
      node.classList.add('started');
      diagram(node);
    }
    for (const node of root.querySelectorAll('.chart:not(.started)')) {
      node.classList.add('started');
      chart(node);
    }
    for (const img of root.querySelectorAll('img.md-img:not(.zoomable)')) {
      img.classList.add('zoomable');
      img.addEventListener('click', () => lightbox(img.src));
    }
  }

  // --- a picture at full size, over everything; a click or Esc closes it ---

  function lightbox(src) {
    const shade = el('div', 'lightbox');
    const img = el('img');
    img.src = src;
    shade.append(img);
    const close = () => { shade.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    shade.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.append(shade);
  }

  // --- the files the assistant shows ---

  const fileUrls = new Map();
  function fileData(call, id) {
    if (!fileUrls.has(id)) {
      fileUrls.set(id, call('file', id).then(({ mime, data }) => {
        const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
        const blob = new Blob([bytes], { type: mime });
        return { url: URL.createObjectURL(blob), blob, mime };
      }).catch((error) => { fileUrls.delete(id); throw error; }));
    }
    return fileUrls.get(id);
  }
  const size = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB`
    : `${(n / 1024 / 1024).toFixed(1)} MB`);
  const TEXT = /^(text\/|application\/(json|xml|x-yaml|yaml|javascript|x-sh|x-python|sql|toml))/;
  const CODE_EXT = /\.(py|js|ts|tsx|jsx|json|ya?ml|toml|sh|sql|go|rs|java|c|cc|cpp|h|hpp|rb|php|css|html?|xml|ini|conf|log|md|txt|env|dockerfile)$/i;
  const LANGS = { py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', yml: 'yaml',
    sh: 'bash', rs: 'rust', cc: 'cpp', hpp: 'cpp', h: 'c', md: 'markdown', htm: 'html' };

  function csvRows(text, limit) {
    const rows = [];
    let row = [];
    let cell = '';
    let quoted = false;
    for (let i = 0; i < text.length && rows.length < limit; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') quoted = false; else cell += c;
      } else if (c === '"') quoted = true;
      else if (c === ',' || c === '\t') { row.push(cell); cell = ''; }
      else if (c === '\n') { row.push(cell.replace(/\r$/, '')); rows.push(row); row = []; cell = ''; }
      else cell += c;
    }
    if ((cell || row.length) && rows.length < limit) { row.push(cell); rows.push(row); }
    return rows;
  }

  // A file in the conversation: pictures, video and sound show where they are; a document, a table or code stays a
  // card, and opens in a preview (laid out) when asked, so a long text does not fill the conversation.
  function fileCard(file, call, onError, markdown) {
    const box = el('div', 'file-card');
    box.dataset.file = file.id;
    const mime = file.mime || '';
    const head = el('div', 'file-head');
    const badge = el('span', 'file-badge', (file.name.split('.').pop() || '?').slice(0, 4).toUpperCase());
    head.append(badge, el('span', 'file-name', file.name), el('span', 'file-size', size(file.size || 0)));
    const actions = el('span', 'file-actions');
    const act = (label, method) => {
      const b = el('button', 'outline small', label);
      b.addEventListener('click', async (event) => {
        event.stopPropagation();
        b.disabled = true;
        try { await call(method, file.id, file.name); } catch (error) { onError(error.message); }
        b.disabled = false;
      });
      actions.append(b);
    };
    const readable = !/^(image|video|audio)\//.test(mime) && (TEXT.test(mime) || CODE_EXT.test(file.name)
      || /\.(csv|tsv|md|markdown)$/i.test(file.name));
    const look = () => preview({ name: file.name, load: () => call('file', file.id), markdown: markdown || plainText,
      open: () => call('openFile', file.id, file.name), save: () => call('saveFile', file.id, file.name), onError });
    if (readable) {
      const b = el('button', 'outline small amber-text', '预览');
      b.addEventListener('click', (event) => { event.stopPropagation(); look(); });
      actions.append(b);
      head.classList.add('opens');
      head.addEventListener('click', look);
      box.classList.add('readable');
    }
    // A readable file opens with the system's app from its preview; the card keeps room for its name.
    if (!readable) act('打开', 'openFile');
    act('另存为', 'saveFile');
    head.title = file.name;
    head.append(actions);
    box.append(head);
    const body = el('div', 'file-body');
    const show = async () => {
      const { url } = await fileData(call, file.id);
      if (mime.startsWith('image/')) {
        const img = el('img', 'file-image');
        img.alt = file.name;
        img.src = url;
        img.addEventListener('click', () => lightbox(url));
        body.replaceChildren(img);
      } else if (mime.startsWith('video/')) {
        const video = el('video', 'file-media');
        video.controls = true;
        video.preload = 'metadata';
        video.src = url;
        body.replaceChildren(video);
      } else if (mime.startsWith('audio/')) {
        const audio = el('audio', 'file-audio');
        audio.controls = true;
        audio.src = url;
        body.replaceChildren(audio);
      } else {
        box.classList.add('plain');
        return;
      }
      box.classList.add('previewed');
    };
    // Pictures, video and sound are fetched at once; anything else waits to be previewed or opened.
    if (/^(image|video|audio)\//.test(mime)) {
      body.append(el('div', 'file-loading', '加载中…'));
      show().catch((error) => body.replaceChildren(el('div', 'rich-error', `没能取回：${error.message}`)));
    } else {
      box.classList.add('plain');
    }
    box.append(body);
    return box;
  }

  // --- a file named in an answer, opened over the conversation: a document laid out, a table, a picture, code ---

  async function preview({ name, path, load, markdown, open, save, onError }) {
    const shade = el('div', 'preview');
    const panel = el('div', 'preview-panel');
    const head = el('div', 'preview-head');
    const title = el('div', 'preview-title');
    title.append(el('b', '', name), el('span', 'preview-path', path || ''));
    const actions = el('div', 'preview-actions');
    const button = (label, fn, cls = 'outline small') => {
      const b = el('button', cls, label);
      b.type = 'button';
      b.addEventListener('click', async () => {
        b.disabled = true;
        try { await fn(); } catch (error) { onError(error.message); }
        b.disabled = false;
      });
      actions.append(b);
    };
    const close = () => { shade.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    if (open) button('打开', open);
    if (save) button('另存为', save);
    button('关闭', close, 'outline small close');
    head.append(title, actions);
    const body = el('div', 'preview-body bubble');
    body.append(el('div', 'file-loading', '加载中…'));
    panel.append(head, body);
    shade.append(panel);
    shade.addEventListener('click', (event) => { if (event.target === shade) close(); });
    document.addEventListener('keydown', onKey);
    document.body.append(shade);
    try {
      const { mime, data } = await load();
      const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
      const blob = new Blob([bytes], { type: mime });
      const ext = (name.split('.').pop() || '').toLowerCase();
      if (mime.startsWith('image/')) {
        const img = el('img', 'file-image');
        img.src = URL.createObjectURL(blob);
        img.alt = name;
        body.replaceChildren(img);
      } else if (/^(video|audio)\//.test(mime)) {
        const media = el(mime.startsWith('video/') ? 'video' : 'audio', 'file-media');
        media.controls = true;
        media.src = URL.createObjectURL(blob);
        body.replaceChildren(media);
      } else if (ext === 'md' || ext === 'markdown' || mime === 'text/markdown') {
        const doc = el('div', 'preview-doc');
        doc.innerHTML = markdown(await blob.text()).html;
        body.replaceChildren(doc);
        enhance(doc);
      } else if (ext === 'csv' || ext === 'tsv' || mime.startsWith('text/csv')) {
        const rows = csvRows(await blob.text(), 2001);
        const table = el('table');
        rows.slice(0, 2000).forEach((cells, i) => {
          const tr = el('tr');
          for (const c of cells) tr.append(el(i ? 'td' : 'th', '', c));
          table.append(tr);
        });
        body.replaceChildren(table);
        if (rows.length > 2000) body.append(el('div', 'file-more', '只显示前 2000 行'));
      } else if (TEXT.test(mime) || CODE_EXT.test(name) || mime === 'application/octet-stream' && blob.size < 2e6) {
        const pre = el('pre');
        const code = el('code', '', await blob.text());
        code.dataset.lang = LANGS[ext] || ext;
        pre.append(code);
        body.replaceChildren(pre);
        highlight(body);
      } else {
        body.replaceChildren(el('div', 'file-more', '这种文件不能在这里预览，点“打开”用电脑上的程序看。'));
      }
    } catch (error) {
      body.replaceChildren(el('div', 'rich-error', `打不开：${error.message}`));
    }
    return shade;
  }

  // Without a Markdown reader, a document shows as its text.
  function plainText(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return { html: `<pre>${div.innerHTML}</pre>` };
  }

  window.Rich = { enhance, fileCard, lightbox, preview };
})();
