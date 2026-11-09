// The window: log in once, then one conversation with the assistant, which the person's projects can narrow.
// Everything goes through window.continuum (preload.js); results come back as { ok, data } or { ok: false, error }.
(function () {
  'use strict';
  const api = window.continuum;
  const $ = (id) => document.getElementById(id);
  const state = {
    user: '', connection: 'offline', view: 'chat',
    // The conversation is one; `focus` is the project it is narrowed to (null: everything).
    // `focus`: the project open on the projects tab (null: the list); `seg`: projects or workers there.
    focus: null, seg: 'projects', messages: new Map(), projects: [],
    // What the workers do: their steps (kept by the server), and what is live only now: the answer being written
    // and the model thinking, per ticket, each with its project.
    steps: new Map(), streams: new Map(), thinking: new Map(),
    // Which step boxes a person opened or closed by hand.
    open: new Map(),
    // What has been on screen already: only what is new plays its entrance.
    shown: new Set(),
  };

  const STAGES = {
    'person/answer': '等你回答', 'person/review': '等你看结果', 'person/blocked': '卡住了',
    'person/failed': '失败了', 'person/delivery': '消息没送到', 'working/queued': '排队中',
    working: '进行中', idle: '等待中',
  };
  const OUTCOMES = { completed: '已完成', stopped: '已停止', dropped: '无需处理', merged: '已合并', closed: '已关闭' };
  const LINK = { online: '已连接', connecting: '连接中', offline: '未连接', unauthorized: '登录已失效' };

  // Stroke icons, drawn inline on a 24 x 24 grid.
  const ICONS = {
    chat: '<path d="M4 5h16v11H9l-5 4z"/>',
    hash: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 20c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="1"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    tag: '<path d="M3 12V3h9l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    check: '<path d="m5 12 5 5 9-10"/>',
    shell: '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="m7 9 3 3-3 3M12 15h5"/>',
    read_file: '<path d="M6 3h9l4 4v14H6z"/><path d="M9 12h7M9 16h7"/>',
    write_file: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13 7 4 4"/>',
    list_files: '<path d="M3 6h6l2 2h10v11H3z"/>',
    think: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M18 16l.7 1.8 1.8.7-1.8.7L18 21l-.7-1.8-1.8-.7 1.8-.7z"/>',
    chevron: '<path d="m9 6 6 6-6 6"/>',
    plug: '<path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0zM12 18v3"/>',
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    repeat: '<path d="M4 12a8 8 0 0 1 14-5.3L20 9"/><path d="M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15"/><path d="M4 20v-5h5"/>',
    claude_code: '<path d="M4 12h11M11 7l5 5-5 5"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
    ssh: '<rect x="3" y="4" width="18" height="7" rx="1"/><rect x="3" y="13" width="18" height="7" rx="1"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    dispatch: '<circle cx="5" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 12h10M7 11l10-5M7 13l10 5"/>',
    codex: '<path d="M4 12h11M11 7l5 5-5 5"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
  };

  // --- avatars: a picture drawn from the name, for anyone who has none of their own ---

  const ASSISTANT = 'continuum';
  const pictures = new Map();
  function picture(name, ai) {
    const key = `${ai ? 8 : 4}:${name}`;
    if (!pictures.has(key)) {
      pictures.set(key, window.ContinuumAvatar
        ? window.ContinuumAvatar.svg(name, ai ? { folds: 8, hues: [165, 285] } : { folds: 4 }) : '');
    }
    return pictures.get(key);
  }
  function avatar(name, ai, extra = '') {
    const node = el('span', `avatar${ai ? ' ai' : ''}${extra}`);
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = picture(name, ai);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'i');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = ICONS[name];
    return svg;
  }

  async function call(method, ...args) {
    const result = await api[method](...args);
    if (!result.ok) throw Object.assign(new Error(result.error || '出错了'), { status: result.status });
    return result.data;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // An English label beside the Chinese: decoration for the eye, hidden from screen readers.
  function en(text) {
    const node = el('span', 'en', text);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function arrival(node, key) {
    if (!state.shown.has(key)) {
      state.shown.add(key);
      node.classList.add('arrived');
    }
    return node;
  }

  const pad = (n) => String(n).padStart(2, '0');
  function time(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function day(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function showStatus(text) {
    $('status').textContent = text || '';
    $('status').hidden = !text;
  }

  // --- a small, safe Markdown: everything is escaped first, then a known set of marks is read: headings, lists,
  // tables, fenced code, quotes, rules, bold, italic, inline code and http(s) links (which open in the browser) ---

  function escape(text) {
    return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function inline(text) {
    // Code spans and links are set aside first, so nothing inside them is read as a mark.
    const kept = [];
    const keep = (html) => `\u0000${kept.push(html) - 1}\u0000`;
    let out = text.replace(/`([^`]+)`/g, (_, code) => keep(`<code>${escape(code)}</code>`));
    out = out.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => keep(`<a href="${escape(url)}" target="_blank" rel="noopener">${escape(label)}</a>`));
    out = out.replace(/https?:\/\/[^\s<>()"'`]+[^\s<>()"'`.,;:!?，。；：！？）]/g,
      (url) => keep(`<a href="${escape(url)}" target="_blank" rel="noopener">${escape(url)}</a>`));
    out = escape(out)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
    return out.replace(/\u0000(\d+)\u0000/g, (_, n) => kept[Number(n)]);
  }
  const ROW = /^\s*\|.*\|\s*$/;
  const RULE = /^\s*\|[\s:|-]+\|\s*$/;
  const ITEM = /^\s*([-*•+]|\d+[.)])\s+/;
  const FENCE = /^\s*(```|~~~)/;
  const HEADING = /^\s*#{1,6}\s+/;
  const QUOTE = /^\s*>\s?/;
  const HR = /^\s*([-*_])(\s*\1){2,}\s*$/;
  function markdown(text) {
    const lines = text.replace(/\r/g, '').split('\n');
    const out = [];
    let rich = false;
    let i = 0;
    const special = (at) => FENCE.test(lines[at]) || HEADING.test(lines[at]) || ITEM.test(lines[at])
      || QUOTE.test(lines[at]) || HR.test(lines[at]) || (ROW.test(lines[at]) && RULE.test(lines[at + 1] || ''));
    while (i < lines.length) {
      const line = lines[i];
      if (FENCE.test(line)) {
        const fence = line.trim().slice(0, 3);
        const code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) code.push(lines[i++]);
        i++;
        out.push(`<pre><code>${escape(code.join('\n'))}</code></pre>`);
        rich = true;
      } else if (ROW.test(line) && RULE.test(lines[i + 1] || '')) {
        const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        const head = cells(line);
        const rows = [];
        i += 2;
        while (i < lines.length && ROW.test(lines[i])) rows.push(cells(lines[i++]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
        rich = true;
      } else if (HR.test(line)) {
        out.push('<hr>');
        i++;
      } else if (ITEM.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const items = [];
        while (i < lines.length && ITEM.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(ITEM, ''))}</li>`);
        out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`);
        rich = true;
      } else if (QUOTE.test(line)) {
        const quoted = [];
        while (i < lines.length && QUOTE.test(lines[i])) quoted.push(inline(lines[i++].replace(QUOTE, '')));
        out.push(`<blockquote>${quoted.join('<br>')}</blockquote>`);
      } else if (HEADING.test(line)) {
        out.push(`<h4>${inline(line.replace(HEADING, ''))}</h4>`);
        rich = true;
        i++;
      } else if (line.trim()) {
        // Lines that follow one another are one paragraph, with their breaks kept.
        const para = [];
        while (i < lines.length && lines[i].trim() && !special(i)) para.push(inline(lines[i++]));
        if (!para.length) para.push(inline(lines[i++]));
        out.push(`<p>${para.join('<br>')}</p>`);
      } else {
        i++;
      }
    }
    return { html: out.join(''), rich };
  }

  // --- login ---

  function setLinkBox(text, note, dot) {
    $('setup-state').textContent = text;
    $('setup-note').textContent = note;
    $('setup-dot').dataset.state = dot;
  }

  function showSetup(server, error) {
    $('main').hidden = true;
    $('setup').hidden = false;
    if (server) $('server').value = server;
    $('setup-error').hidden = !error;
    $('setup-error').textContent = error || '';
    setLinkBox('未连接', '等待建立连接…', 'offline');
    ($('username').value ? $('password') : $('username')).focus();
  }

  $('reveal').addEventListener('click', () => {
    const shown = $('password').type === 'text';
    $('password').type = shown ? 'password' : 'text';
    $('reveal').setAttribute('aria-pressed', String(!shown));
    $('reveal').title = shown ? '显示密码' : '隐藏密码';
  });

  $('setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('connect').disabled = true;
    $('setup-error').hidden = true;
    setLinkBox('连接中', '正在验证账号…', 'connecting');
    try {
      const saved = await call('saveConfig', {
        server: $('server').value.trim(), username: $('username').value.trim(), password: $('password').value,
      });
      $('password').value = '';
      await showMain(saved.user);
    } catch (error) {
      $('setup-error').textContent = error.status === 401 ? '用户名或密码不对。' : error.message;
      $('setup-error').hidden = false;
      setLinkBox('未连接', error.status === 0 ? '连不上服务器' : '登录没有成功', 'offline');
      $('password').select();
    } finally {
      $('connect').disabled = false;
    }
  });

  $('menu').addEventListener('click', async () => {
    await call('logout');
    state.messages.clear();
    state.projects = [];
    state.shown.clear();
    state.steps.clear();
    state.streams.clear();
    state.thinking.clear();
    state.focus = null;
    showSetup();
  });

  // --- main ---

  async function showMain(user) {
    state.user = user;
    $('user').textContent = user;
    $('me-avatar').innerHTML = picture(user, false);
    $('setup').hidden = true;
    $('main').hidden = false;
    if (api.setTimezone) {
      // Schedules fall on the person's own clock.
      call('setTimezone', Intl.DateTimeFormat().resolvedOptions().timeZone).catch(() => {});
    }
    await loadAll();
    renderMessages(true);
    $('input').focus();
    // Every login asks whether a newer version is out.
    checkUpdate(false);
    if (api.workers) loadWorkers();
  }

  function setView(view) {
    state.view = view;
    for (const button of document.querySelectorAll('.tabs button')) {
      button.setAttribute('aria-selected', String(button.dataset.view === view));
    }
    $('view-chat').hidden = view !== 'chat';
    $('view-projects').hidden = view !== 'projects';
    $('view-settings').hidden = view !== 'settings';
    $('view-tools').hidden = view !== 'tools';
    if (view === 'tools') loadToolsTab();
    $('settings').setAttribute('aria-pressed', String(view === 'settings'));
    if (view === 'projects') showProjectsTab();
    if (view === 'chat') {
      renderMessages(true);
      $('input').focus();
    }

  }

  // The projects tab: the list of projects or of workers, or one project with its own part of the conversation.
  function showProjectsTab() {
    const project = state.focus ? projectById(state.focus) : null;
    $('projects-overview').hidden = Boolean(project);
    $('project-detail').hidden = !project;
    for (const button of document.querySelectorAll('.segments button')) {
      button.setAttribute('aria-selected', String(button.dataset.seg === state.seg));
    }
    $('panel-projects').hidden = state.seg !== 'projects';
    $('panel-workers').hidden = state.seg !== 'workers';
    if (project) renderProjectDetail(project);
    else if (state.seg === 'workers') loadWorkers();
  }
  for (const button of document.querySelectorAll('.segments button')) {
    button.addEventListener('click', () => { state.seg = button.dataset.seg; showProjectsTab(); });
  }
  for (const button of document.querySelectorAll('.tabs button')) {
    button.addEventListener('click', () => setView(button.dataset.view));
  }

  // --- projects: what the person's work is about. The conversation is one; opening a project narrows it ---

  const PROJECT_STATES = {
    proposed: '等你确认', active: '进行中', waiting: '等你回答', paused: '已暂停', ended: '已结束', refused: '没有采用',
  };

  function projectById(id) {
    return state.projects.find((p) => p.id === id) || null;
  }

  async function loadProjects() {
    try {
      state.projects = await call('projects');
    } catch (error) {
      showStatus(error.message);
    }
    renderProjects();
    if (state.view === 'projects' && state.focus) {
      const project = projectById(state.focus);
      if (project) $('project-card').replaceChildren(projectCard(project, true));
    }
    // Messages that name a project (a proposal, a reminder) read better once the project is known.
    if (!$('main').hidden) renderMessages();
  }

  let projectsSoon = null;
  function projectsChanged() {
    if (projectsSoon) return;
    projectsSoon = setTimeout(() => { projectsSoon = null; loadProjects(); }, 400);
  }

  function openProject(id) {
    state.focus = id;
    setView('projects');
  }

  $('project-back').addEventListener('click', () => {
    state.focus = null;
    showProjectsTab();
  });

  function renderProjectDetail(project) {
    $('project-card').replaceChildren(projectCard(project, true));
    const closed = project.kind === 'ongoing' && (project.state === 'ended' || project.state === 'refused');
    $('project-input').placeholder = closed ? '这个项目已经结束了' : `对「${project.name}」说（仍在主对话里）`;
    $('project-input').disabled = closed;
    renderMessages(true);
  }

  $('project-composer').addEventListener('submit', async (event) => {
    event.preventDefault();
    const project = projectById(state.focus);
    const text = $('project-input').value.trim();
    if (!text || !project) return;
    $('project-send').disabled = true;
    try {
      const row = await call('post', project.thread || 'main', text);
      $('project-input').value = '';
      addMessage(row);
      showStatus('');
    } catch (error) {
      showStatus(`没发出去：${error.message}`);
    } finally {
      $('project-send').disabled = false;
    }
  });
  $('project-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('project-composer').requestSubmit();
    }
  });

  // --- the conversation: everything said, each message with the project it belongs to ---

  async function loadAll() {
    const known = [...state.messages.keys()];
    const after = known.length ? Math.max(...known) : 0;
    try {
      const [rows, steps] = await Promise.all([call('everything', after), call('allSteps')]);
      for (const row of rows) addMessage(row, false);
      state.steps = new Map(steps.map((step) => [`${step.ticket_id}:${step.key}`, step]));
    } catch (error) {
      showStatus(error.message);
    }
    await loadProjects();
  }

  // Whether something of this project is on screen: the conversation shows everything; an open project, its own.
  function visible(project) {
    return state.view === 'chat' || (state.view === 'projects' && state.focus !== null && project === state.focus);
  }

  function addMessage(row, render = true) {
    state.messages.set(row.id, row);
    // The answer arrived: what was being written gives way to it.
    if (row.ticket_id && !['user', 'acknowledgement', 'scheduled'].includes(row.role)) state.streams.delete(row.ticket_id);
    if (row.project && !projectById(row.project)) projectsChanged();
    if (render) renderMessages();
  }

  // A worker asks the person to connect a tool: the form is right here, and what is typed goes to the server,
  // never into the conversation.
  const openForms = new Map();
  function credential(m) {
    const meta = m.meta || {};
    const form = TOOL_FORMS[meta.kind] || { name: meta.kind, hint: '', fields: [] };
    const item = el('li', 'msg proposal credential');
    item.dataset.id = m.id;
    const card = el('div', 'proposal-card');
    const head = el('div', 'proposal-head');
    head.append(icon('plug'), el('span', '', `需要你连接 ${form.name}`));
    card.append(head);
    if (meta.reason) card.append(el('p', 'credential-reason', `用来：${meta.reason}`));
    if (meta.state === 'connected') {
      card.append(el('span', 'decided connected', `已连接${meta.account ? `：${meta.account}` : ''}`));
    } else if (state.opened === m.id) {
      // The same form across redraws: the conversation redraws on every event, and what is typed must stay.
      let filling = openForms.get(m.id);
      if (!filling) {
        filling = toolForm({ kind: meta.kind, connected: false, settings: {} }, form, (updated) => {
          state.opened = null;
          openForms.delete(m.id);
          if (updated) meta.state = 'connected';
          renderMessages();
        });
        openForms.set(m.id, filling);
      }
      card.append(filling);
    } else {
      const actions = el('div', 'actions');
      const go = el('button', 'outline amber', '填写凭据');
      go.addEventListener('click', () => { state.opened = m.id; renderMessages(); });
      actions.append(go);
      card.append(el('p', 'hint', '凭据直接存到服务器，不会出现在对话里，也不会发给模型。'), actions);
    }
    item.append(avatar(ASSISTANT, true), card, el('div', 'meta', time(m.at)));
    return item;
  }

  function message(m) {
    if (m.role === 'proposal') return proposal(m);
    if (m.role === 'credential') return credential(m);
    const item = el('li', `msg ${m.role}`);
    item.dataset.id = m.id;
    const mine = m.role === 'user';
    const face = mine ? avatar(m.user || state.user, false) : avatar(ASSISTANT, true);
    const bubble = el('div', 'bubble');
    if (m.role === 'reply') {
      const { html, rich } = markdown(m.text);
      bubble.innerHTML = html;
      if (rich) item.classList.add('rich');
    } else if (m.role === 'reminder') {
      bubble.append(icon('bell'), el('span', '', m.text));
    } else if (m.role === 'scheduled') {
      bubble.append(icon('repeat'), el('span', '', `定时任务：${m.text.replace(/^【定时任务】/, '')}`));
    } else {
      bubble.textContent = m.text;
    }
    const meta = el('div', 'meta', time(m.at));
    // A message the dispatcher handed to a project already under way says which.
    if (mine && m.meta && m.meta.project && !(m.thread || '').startsWith('p-') && timelineBox()?.box === $('messages')) {
      meta.append(el('span', 'handed', ` · 交给项目「${m.meta.project_name || '项目'}」`));
    }
    item.append(face, bubble, meta);
    return arrival(item, `m:${m.id}`);
  }

  // A long-running project the assistant proposes: what it is for, what it may do, when it acts; the person decides.
  function proposal(m) {
    const meta = m.meta || {};
    const project = projectById(meta.project);
    let decided = meta.state && meta.state !== 'proposed' ? meta.state : null;
    if (!decided && project && project.state !== 'proposed') decided = project.state;
    const item = el('li', 'msg proposal');
    item.dataset.id = m.id;
    const card = el('div', 'proposal-card');
    const head = el('div', 'proposal-head');
    head.append(icon('repeat'), el('span', '', `提议一个长期项目：${meta.project_name || (project && project.name) || ''}`));
    card.append(head);
    if (project) {
      const detail = el('dl', 'detail');
      const row = (label, value) => { if (value) detail.append(el('dt', '', label), el('dd', '', value)); };
      row('目标', project.goal);
      row('可以自己做', project.charter);
      row('安排', (project.schedules || [])
        .map((s) => `${s.text_when} · ${s.action === 'run' ? '开工' : '提醒'}：${s.text}`).join('\n'));
      if (project.ends_at) row('到期', day(project.ends_at));
      card.append(detail);
    }
    const foot = el('div', 'actions');
    if (decided) {
      foot.append(el('span', `decided ${decided}`,
        decided === 'refused' ? '没有采用' : `已确认 · ${PROJECT_STATES[decided] || decided}`));
    } else {
      const answer = (action) => async () => {
        for (const b of foot.querySelectorAll('button')) b.disabled = true;
        try {
          await call('projectAction', meta.project, action);
          await loadProjects();
          renderMessages();
        } catch (error) {
          showStatus(error.message);
          for (const b of foot.querySelectorAll('button')) b.disabled = false;
        }
      };
      const no = el('button', 'outline', '不要');
      no.addEventListener('click', answer('refuse'));
      const yes = el('button', 'outline amber', '确认开始');
      yes.addEventListener('click', answer('confirm'));
      foot.append(no, yes);
    }
    card.append(foot);
    item.append(avatar(ASSISTANT, true), card, el('div', 'meta', time(m.at)));
    return arrival(item, `m:${m.id}:${decided || ''}`);
  }

  // --- what the worker does: its steps, folded into one box per ticket, the way a chat app shows tool calls ---

  const STEP_NAMES = {
    shell: '运行命令', read_file: '读取文件', write_file: '写入文件', list_files: '查看目录',
    claude_code: '交给 Claude Code', codex: '交给 Codex', ssh: '远程命令', dispatch: '派出 Worker',
    github: 'GitHub', email: '邮箱', slack: 'Slack', google: 'Google', cloudflare: 'Cloudflare', godaddy: 'GoDaddy',
    aws: 'AWS', request_tool: '请你连接工具',
  };

  function remember(details, key, fallback) {
    details.open = state.open.has(key) ? state.open.get(key) : fallback;
    details.addEventListener('toggle', () => state.open.set(key, details.open));
    return details;
  }

  // A command on one of the person's servers that waits for them: what, where, why, and their two answers.
  function approvalItem(step) {
    const item = el('li', 'step ssh approval');
    const box = el('div', 'approval-box');
    const head = el('div', 'approval-head');
    head.append(icon('ssh'), el('span', 'step-name', '等你批准一条远程命令'));
    const [host, ...rest] = step.input.split('  $ ');
    const where = el('div', 'approval-host', `服务器 ${host}`);
    const command = el('pre', 'step-command', rest.join('  $ ') || step.input);
    box.append(head, where, command);
    if (step.output) box.append(el('p', 'approval-reason', `理由：${step.output}`));
    const actions = el('div', 'actions');
    const answer = (approve) => async () => {
      for (const b of actions.querySelectorAll('button')) b.disabled = true;
      try {
        await call('approve', step.ticket_id, step.key, approve);
        showStatus('');
      } catch (error) {
        showStatus(error.message);
        for (const b of actions.querySelectorAll('button')) b.disabled = false;
      }
    };
    const refuse = el('button', 'outline danger', '拒绝');
    refuse.addEventListener('click', answer(false));
    const run = el('button', 'outline amber', '批准执行');
    run.addEventListener('click', answer(true));
    actions.append(refuse, run);
    box.append(actions);
    item.append(box);
    return item;
  }

  function stepItem(step) {
    if (step.status === 'approval') return approvalItem(step);
    const item = el('li', `step ${step.name} ${step.status}`);
    if (step.name === 'note') {
      item.append(el('p', 'note', step.output));
      return item;
    }
    const details = remember(el('details'), `s:${step.ticket_id}:${step.key}`, false);
    const summary = el('summary');
    // A step one of the lead's sub-workers took says which one.
    if (step.worker) summary.append(el('span', 'step-worker', step.worker));
    const TOOL_STEPS = ['github', 'email', 'slack', 'google', 'cloudflare', 'godaddy', 'aws', 'request_tool'];
    summary.append(icon(TOOL_STEPS.includes(step.name) ? 'plug' : STEP_NAMES[step.name] ? step.name : 'think'));
    if (step.name === 'think') {
      summary.append(el('span', 'step-name', `思考了 ${step.input || 1} 秒`));
    } else {
      summary.append(el('span', 'step-name', STEP_NAMES[step.name] || step.name), el('code', 'step-input', step.input));
    }
    const mark = el('span', 'step-state');
    mark.title = { running: '进行中', done: '完成', error: '出错' }[step.status] || '';
    if (step.status === 'done') mark.append(icon('check'));
    summary.append(mark);
    details.append(summary);
    // The summary shows a command's first two lines; opened, the step shows all of it.
    if (step.name !== 'think' && step.input && (step.input.length > 60 || step.input.includes('\n'))) {
      details.append(el('pre', 'step-command', step.input));
    }
    if (step.output) details.append(el(step.name === 'think' ? 'div' : 'pre', 'step-output', step.output));
    else if (step.status === 'running') details.append(el('div', 'step-output waiting', '等待输出…'));
    item.append(details);
    return item;
  }

  // Thinking twice in a row (with only the worker's own calls, which are not shown, in between) is one thought.
  function mergeThinking(steps) {
    const out = [];
    for (const step of steps) {
      const last = out[out.length - 1];
      if (step.name === 'think' && last && last.name === 'think' && (last.worker || '') === (step.worker || '')) {
        out[out.length - 1] = { ...last, input: String((Number(last.input) || 0) + (Number(step.input) || 0)),
          output: [last.output, step.output].filter(Boolean).join('\n\n') };
      } else {
        out.push(step);
      }
    }
    return out;
  }

  // Where a run of work stands, in a few words: what it waits for, what it is doing, or what it did.
  function workState(steps, thinking) {
    const asking = steps.some((s) => s.status === 'approval');
    const running = thinking || asking || steps.some((s) => s.status === 'running');
    const counted = steps.filter((s) => s.name !== 'note' && s.name !== 'think').length;
    const current = [...steps].reverse().find((s) => s.status === 'running');
    const busy = { claude_code: 'Claude Code 正在处理…', codex: 'Codex 正在处理…' };
    const dispatching = steps.find((s) => s.name === 'dispatch' && s.status === 'running');
    let label;
    if (asking) label = '等你批准一条远程命令';
    else if (dispatching) label = `${(dispatching.output || '').split('\n').filter(Boolean).length} 个 Worker 正在同时干活…`;
    else if (current) label = busy[current.name] || `正在${STEP_NAMES[current.name] || '处理'}…`;
    else if (thinking) label = '思考中…';
    else label = counted ? `已执行 ${counted} 个步骤` : '已思考';
    return { asking, running, current, label };
  }

  // In the main conversation a project's work is one line: the dispatcher handed it to a project, which does it and
  // comes back with the result. Its steps are on the project's page; a command waiting for the person shows here.
  function projectLine(ticket, steps, thinking) {
    const { asking, running, label } = workState(steps, thinking);
    const project = (steps.find((s) => s.project) || {}).project || ticket;
    const item = el('li', `work compact${running ? ' running' : ''}`);
    item.dataset.ticket = ticket;
    const line = el('div', 'work-line');
    const known = projectById(project);
    line.append(el('span', running ? 'work-spin' : 'work-done'), el('span', 'work-project', known ? known.name : '项目'),
      el('span', 'work-label', label));
    const open = el('button', 'outline small', '查看过程');
    open.addEventListener('click', () => openProject(project));
    line.append(open);
    item.append(line);
    if (asking) {
      const list = el('ol', 'work-steps waiting');
      list.append(...steps.filter((s) => s.status === 'approval').map(stepItem));
      item.append(list);
    }
    return arrival(item, `w:${ticket}`);
  }

  function workBox(ticket, steps, thinking) {
    const { asking, running, current, label } = workState(steps, thinking);
    const item = el('li', `work${running ? ' running' : ''}`);
    item.dataset.ticket = ticket;
    const details = remember(el('details', 'work-box'), `w:${ticket}`, running);
    if (running) details.open = state.open.get(`w:${ticket}`) !== false;
    if (asking) details.open = true;
    const summary = el('summary');
    summary.append(el('span', running ? 'work-spin' : 'work-done'), el('span', 'work-label', label), icon('chevron'));
    details.append(summary);
    const list = el('ol', 'work-steps');
    list.append(...mergeThinking(steps).map(stepItem));
    // Waiting for the person is not thinking.
    if (thinking && !current && !asking) list.append(el('li', 'step thinking', '思考中…'));
    details.append(list);
    item.append(details);
    return arrival(item, `w:${ticket}`);
  }

  function streamItem(ticket, text) {
    const item = el('li', 'msg reply streaming');
    item.id = `stream-${ticket}`;
    const bubble = el('div', 'bubble');
    const { html, rich } = markdown(text);
    bubble.innerHTML = html;
    if (rich) item.classList.add('rich');
    item.append(avatar(ASSISTANT, true), bubble);
    return item;
  }

  // The conversation where it is on screen: all of it on the conversation tab, an open project's part on its page.
  function timelineBox() {
    if (state.view === 'chat') return { box: $('messages'), scroller: $('messages') };
    if (state.view === 'projects' && state.focus) return { box: $('project-messages'), scroller: $('view-projects') };
    return null;
  }

  function renderMessages(toBottom = false) {
    const where = timelineBox();
    if (!where) return;
    const { box, scroller } = where;
    // Only one place shows the conversation at a time, so the live parts (an answer being written) are not doubled.
    for (const other of [$('messages'), $('project-messages')]) if (other !== box) other.replaceChildren();
    const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
    const list = [...state.messages.values()].filter((m) => visible(m.project)).sort((a, b) => a.id - b.id);
    const steps = [...state.steps.values()].filter((s) => visible(s.project));
    // Messages and steps in the order they happened; the steps of one ticket in a row share a box.
    const timeline = [...list.map((m) => ({ at: m.at, m })), ...steps.map((s) => ({ at: s.at, s }))]
      .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    const nodes = [];
    const thinkingHere = new Set([...state.thinking].filter(([, project]) => visible(project)).map(([t]) => t));
    let run = null;
    const flush = () => {
      if (!run) return;
      const draw = box === $('messages') ? projectLine : workBox;
      nodes.push(draw(run.ticket, run.steps, thinkingHere.has(run.ticket) && run.last));
      run = null;
    };
    timeline.forEach((entry, index) => {
      if (entry.m) {
        flush();
        nodes.push(message(entry.m));
        return;
      }
      if (!run || run.ticket !== entry.s.ticket_id) {
        flush();
        run = { ticket: entry.s.ticket_id, steps: [] };
      }
      run.steps.push(entry.s);
      run.last = !timeline.slice(index + 1).some((e) => (e.m ? e.m.ticket_id : e.s.ticket_id) === run.ticket);
    });
    flush();
    // A ticket thinking with no step of its own after its last message yet gets a box of its own at the end.
    for (const ticket of thinkingHere) {
      if (!nodes.some((n) => n.classList.contains('running') && n.dataset.ticket === ticket)) {
        nodes.push((box === $('messages') ? projectLine : workBox)(ticket, [], true));
      }
    }
    for (const [ticket, live] of state.streams) {
      if (visible(live.project)) nodes.push(streamItem(ticket, live.text));
    }
    box.replaceChildren(...nodes);
    if (box === $('messages')) $('empty-chat').hidden = nodes.length > 0;
    if (toBottom || near) scroller.scrollTop = scroller.scrollHeight;
  }

  // The answer as it is written: only its bubble changes, so the rest of the conversation stays as it is.
  function updateStream(ticket) {
    const live = state.streams.get(ticket);
    const node = document.getElementById(`stream-${ticket}`);
    if (!live || !node || !visible(live.project)) {
      if (live && visible(live.project)) renderMessages();
      return;
    }
    const { scroller } = timelineBox() || { scroller: $('messages') };
    const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
    const { html, rich } = markdown(live.text);
    node.querySelector('.bubble').innerHTML = html;
    node.classList.toggle('rich', rich);
    if (near) scroller.scrollTop = scroller.scrollHeight;
  }

  $('composer').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = $('input').value.trim();
    if (!text) return;
    $('send').disabled = true;
    try {
      const row = await call('post', 'main', text);
      $('input').value = '';
      autosize();
      addMessage(row);
      showStatus('');
    } catch (error) {
      showStatus(`没发出去：${error.message}`);
    } finally {
      $('send').disabled = false;
      $('input').focus();
    }
  });

  function autosize() {
    const input = $('input');
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight + 2, 160)}px`;
  }
  $('input').addEventListener('input', autosize);
  $('input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('composer').requestSubmit();
    }
  });

  // --- search: the whole conversation, on the server, so older messages are found too ---

  const WHO = {
    user: '你', reply: '助理', acknowledgement: '助理', reminder: '提醒', scheduled: '定时任务', notice: '助理',
    proposal: '项目提议',
  };

  // The words around the first match, escaped, with every match marked.
  function snippet(text, query) {
    const at = text.toLowerCase().indexOf(query.toLowerCase());
    const start = Math.max(0, at - 30);
    const part = (start ? '…' : '') + text.slice(start, start + 140) + (start + 140 < text.length ? '…' : '');
    const lower = part.toLowerCase();
    const needle = query.toLowerCase();
    let out = '';
    let i = 0;
    for (let j = lower.indexOf(needle); j >= 0; j = lower.indexOf(needle, j + needle.length)) {
      out += escape(part.slice(i, j)) + `<mark>${escape(part.slice(j, j + needle.length))}</mark>`;
      i = j + needle.length;
    }
    return out + escape(part.slice(i));
  }

  let searchTimer = null;
  let searchSeq = 0;
  async function runSearch() {
    const query = $('search').value.trim();
    const box = $('search-results');
    if (!query) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    const seq = ++searchSeq;
    let rows;
    try {
      rows = await call('search', query);
    } catch (error) {
      rows = [];
      showStatus(error.message);
    }
    if (seq !== searchSeq) return;  // a later search has started
    box.replaceChildren();
    box.hidden = false;
    if (!rows.length) {
      box.append(el('p', 'search-none', `没有找到「${query}」`));
      return;
    }
    box.append(el('p', 'search-count', `找到 ${rows.length}${rows.length >= 50 ? '+' : ''} 条`));
    for (const m of rows) {
      const item = el('button', 'search-hit');
      item.setAttribute('role', 'option');
      item.dataset.id = m.id;
      const head = el('span', 'search-head');
      head.append(el('b', '', WHO[m.role] || m.role), el('span', '', `${day(m.at)} ${time(m.at)}`));
      const text = el('span', 'search-text');
      text.innerHTML = snippet(m.text, query);
      item.append(head, text);
      item.addEventListener('click', () => jumpTo(m, item));
      box.append(item);
    }
  }

  // Go to a found message in the conversation; one older than what is loaded opens in place instead.
  function jumpTo(m, item) {
    const node = document.querySelector(`#messages [data-id="${Number(m.id)}"]`);
    if (!node) {
      item.classList.toggle('open');
      item.querySelector('.search-text').textContent = item.classList.contains('open') ? m.text : '';
      if (!item.classList.contains('open')) item.querySelector('.search-text').innerHTML = snippet(m.text, $('search').value.trim());
      return;
    }
    $('search-results').hidden = true;
    node.scrollIntoView({ block: 'center' });
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
  }

  $('search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 250);
  });
  $('search').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      $('search').value = '';
      runSearch();
    } else if (event.key === 'Enter') {
      clearTimeout(searchTimer);
      runSearch();
    }
  });
  $('search').addEventListener('focus', () => { if ($('search').value.trim()) $('search-results').hidden = false; });

  // --- the projects page: what waits for the person first, then what runs on its own, then the rest ---

  function stageText(p) {
    if (p.kind === 'once' && p.state === 'ended') return OUTCOMES[p.outcome] || '已结束';
    if (p.kind === 'once' && p.stage) return STAGES[p.action ? `${p.stage}/${p.action}` : p.stage] || STAGES[p.stage] || p.stage;
    return PROJECT_STATES[p.state] || p.state;
  }

  function needsPerson(p) {
    return p.state === 'proposed' || p.state === 'waiting' || Boolean(p.question);
  }

  function projectCard(p, open = false) {
    const kind = needsPerson(p) ? 'person' : p.state === 'ended' || p.state === 'refused' ? 'ended' : 'working';
    const box = el('article', `card notched project ${kind} ${p.kind}`);
    box.dataset.project = p.id;
    if (p.state === 'ended' && (p.kind === 'ongoing' || p.outcome === 'completed')) {
      const check = el('span', 'check');
      check.append(icon('check'));
      box.append(check);
    }
    const top = el('div', 'card-top');
    top.append(el('span', 'ticket-id', `${p.kind === 'ongoing' ? '长期项目' : '一次性'} · #${p.id.slice(-4).toUpperCase()}`),
      el('span', 'stage', stageText(p)));
    box.append(top, el('div', 'card-title', p.name));
    const detail = el('dl', 'detail');
    const row = (label, value, cls = '') => { if (value) detail.append(el('dt', '', label), el('dd', cls, value)); };
    if (p.question) row('需要你回答', p.question, 'question');
    if (p.kind === 'ongoing') {
      row('目标', p.goal);
      row('安排', (p.schedules || []).map((s) => `${s.text_when} · ${s.action === 'run' ? '开工' : '提醒'}：${s.text}`).join('\n'));
      if (p.next_at) row('下一次', `${day(p.next_at)} ${time(p.next_at)}`);
      row('运行', `${p.runs} 次${p.busy ? `，${p.busy} 个 Worker 正在干活` : ''}`);
    } else if (p.busy) {
      row('现在', 'Worker 正在干活');
    }
    if (detail.childNodes.length) box.append(detail);

    const foot = el('div', 'card-foot');
    const when = el('span', 'meta-item');
    when.append(icon('calendar'), el('span', '', day(p.last_at || p.updated_at || p.created_at)));
    foot.append(when);
    const actions = el('div', 'actions');
    const button = (text, cls, fn) => {
      const b = el('button', cls, text);
      b.addEventListener('click', fn);
      actions.append(b);
      return b;
    };
    const twice = (b, label, fn) => {
      b.addEventListener('click', () => {
        if (b.dataset.confirm) fn();
        else { b.dataset.confirm = '1'; b.textContent = label; }
      });
    };
    if (p.state === 'proposed') {
      button('不要', 'outline', () => projectAct(p.id, 'refuse'));
      button('确认开始', 'outline amber', () => projectAct(p.id, 'confirm'));
    } else {
      if (!open) button(p.question ? '去回答' : '打开', p.question ? 'outline amber' : 'outline', () => openProject(p.id));
      if (p.kind === 'ongoing' && p.state === 'active') button('暂停', 'outline', () => projectAct(p.id, 'pause'));
      if (p.kind === 'ongoing' && p.state === 'paused') button('继续', 'outline', () => projectAct(p.id, 'resume'));
      if (p.kind === 'ongoing' && (p.state === 'active' || p.state === 'paused')) {
        const end = button('结束', 'outline danger', () => {});
        twice(end, '确认结束？', () => projectAct(p.id, 'end'));
      }
      if (p.kind === 'once' && p.can_retry) button('重试', 'outline', () => act('retry', p.id));
      if (p.kind === 'once' && p.state !== 'ended') {
        const stop = button('停止', 'outline danger', () => {});
        twice(stop, '确认停止？', () => act('stop', p.id));
      }
    }
    foot.append(actions);
    box.append(foot);
    return arrival(box, `p:${p.id}:${p.state}:${p.outcome || ''}`);
  }

  async function projectAct(id, action) {
    try {
      await call('projectAction', id, action);
      showStatus('');
    } catch (error) {
      showStatus(error.message);
    }
    await loadProjects();
    renderMessages();
  }

  async function act(method, id) {
    try {
      await call(method, id);
      showStatus('');
    } catch (error) {
      showStatus(error.message);
    }
    await loadProjects();
  }

  function renderProjects() {
    const all = state.projects;
    const groups = [
      ['等你处理', 'NEEDS YOU', 'person', all.filter((p) => needsPerson(p) && p.state !== 'ended')],
      ['长期项目', 'ONGOING', 'working', all.filter((p) => p.kind === 'ongoing' && !needsPerson(p) && ['active', 'paused'].includes(p.state))],
      ['进行中', 'IN PROGRESS', 'working', all.filter((p) => p.kind === 'once' && !needsPerson(p) && p.state !== 'ended')],
      ['已归档', 'ARCHIVED', 'ended', all.filter((p) => p.state === 'ended').slice(0, 12)],
    ];
    const box = $('projects');
    box.replaceChildren();
    for (const [name, label, kind, items] of groups) {
      if (!items.length) continue;
      const row = el('div', `group-row ${kind}`);
      row.append(el('h3', 'group', `${name} ${pad(items.length)}`), en(label));
      box.append(row, ...items.map((p) => projectCard(p)));
    }
    $('empty-projects').hidden = all.length > 0;
    const waiting = groups[0][3].length;
    $('badge').hidden = waiting === 0;
    $('badge').textContent = String(waiting);
  }

  // --- workers: how many run now, on what, and what the past ones used ---

  const TOOL_NAMES = {
    claude_code: 'Claude Code', codex: 'Codex', ssh: '远程命令', shell: '本地命令', files: '文件操作',
    github: 'GitHub', email: '邮箱', slack: 'Slack', google: 'Google', cloudflare: 'Cloudflare', godaddy: 'GoDaddy',
    aws: 'AWS',
  };
  const FILE_STEPS = new Set(['read_file', 'write_file', 'list_files']);

  function duration(from, to) {
    const ms = (to ? new Date(to) : new Date()) - new Date(from);
    if (!(ms >= 0)) return '';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
    return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  }

  function toolsUsed(steps) {
    const merged = {};
    for (const [name, n] of Object.entries(steps || {})) {
      const key = FILE_STEPS.has(name) ? 'files' : name;
      merged[key] = (merged[key] || 0) + n;
    }
    return merged;
  }

  function tile(value, label, cls = '') {
    const box = el('div', `stat ${cls}`);
    box.append(el('b', '', String(value)), el('span', '', label));
    return box;
  }

  function workerCard(w, live) {
    const kind = live ? (w.busy ? 'busy' : 'idle') : 'ended';
    const box = el('article', `card notched worker ${kind}`);
    const top = el('div', 'card-top');
    top.append(el('span', 'ticket-id', `#${w.ticket_id.slice(-4).toUpperCase()} · 第 ${w.number} 次`),
      el('span', 'stage', live ? (w.busy ? '干活中' : '等待中') : (OUTCOMES[w.outcome] || '已结束')));
    box.append(top, el('div', 'card-title', w.title));
    const detail = el('dl', 'detail');
    const row = (label, value) => { if (value) detail.append(el('dt', '', label), el('dd', '', value)); };
    if (w.current) row('当前步骤', `${STEP_NAMES[w.current.name] || w.current.name}  ${w.current.input}`);
    if (live && !w.busy) row('状态', '这一轮做完了，等下一条消息或回答');
    if (w.crew) row('子 Worker', `${w.crew} 个正在同时干活`);
    if (w.started_at) row(live ? '已运行' : '用时', duration(w.started_at, live ? null : w.ended_at));
    const used = Object.entries(toolsUsed(w.steps)).map(([k, n]) => `${TOOL_NAMES[k] || k} ${n}`).join(' · ');
    row('用到', used);
    box.append(detail);
    const foot = el('div', 'card-foot');
    const when = el('span', 'meta-item');
    when.append(icon('calendar'), el('span', '', `${day(w.started_at || w.created_at)} ${time(w.started_at || w.created_at)}`));
    foot.append(when);
    if (w.thread) {
      const actions = el('div', 'actions');
      const open = el('button', 'outline', '查看对话');
      open.addEventListener('click', () => {
        const ongoing = w.thread && w.thread.startsWith('p-') ? w.thread.slice(2) : null;
        openProject(ongoing && projectById(ongoing) ? ongoing : w.ticket_id);
      });
      actions.append(open);
      foot.append(actions);
    }
    box.append(foot);
    return box;
  }

  let workersTimer = null;
  async function loadWorkers() {
    clearTimeout(workersTimer);
    let page;
    try {
      page = await call('workers');
    } catch (error) {
      showStatus(error.message);
      return;
    }
    $('busy').hidden = !page.busy_now;
    $('busy').textContent = String(page.busy_now);
    if (state.view !== 'projects' || state.seg !== 'workers' || state.focus) return;
    $('worker-stats').replaceChildren(
      tile(`${page.busy_now}/${page.max_workers}`, '正在干活 / 上限', page.busy_now ? 'live' : ''),
      tile(page.totals.waiting, '等待中'),
      tile(page.totals.assignments, '累计调用 Worker'),
      tile(page.totals.tickets, '工单'));
    const tools = toolsUsed(page.tools);
    // The everyday tools always; a connected service once it has been used.
    const everyday = ['claude_code', 'codex', 'ssh', 'shell', 'files'];
    $('worker-tools').replaceChildren(...Object.keys(TOOL_NAMES).filter((k) => everyday.includes(k) || tools[k])
      .map((k) => tile(tools[k] || 0, TOOL_NAMES[k], 'small')));
    const box = $('workers');
    box.replaceChildren();
    const group = (name, label, kind, items, live) => {
      const row = el('div', `group-row ${kind}`);
      row.append(el('h3', 'group', `${name} ${pad(items.length)}`), en(label));
      box.append(row, ...(items.length ? items.map((w) => workerCard(w, live)) : [el('p', 'hint', '没有。')]));
    };
    group('运行中', 'RUNNING', 'working', page.running, true);
    if ((page.crew || []).length || page.crew_waiting) {
      const row = el('div', 'group-row working');
      row.append(el('h3', 'group', `子 Worker ${pad((page.crew || []).length)}`), en('CREW'));
      box.append(row);
      if (page.crew_waiting) box.append(el('p', 'hint', `还有 ${page.crew_waiting} 个在排队等空位。`));
      for (const c of page.crew || []) {
        const item = el('article', 'card notched worker busy crew');
        const top = el('div', 'card-top');
        top.append(el('span', 'ticket-id', c.label), el('span', 'stage', '干活中'));
        const detail = el('dl', 'detail');
        const row2 = (label, value) => { if (value) detail.append(el('dt', '', label), el('dd', '', value)); };
        row2('属于', c.title);
        row2('任务', c.task);
        if (c.step) row2('当前步骤', `${STEP_NAMES[c.step.name] || c.step.name}  ${c.step.input}`);
        row2('已运行', duration(c.started_at));
        item.append(top, detail);
        box.append(item);
      }
    }
    group('最近', 'RECENT', 'ended', page.recent, false);
    // While the page is open it keeps itself current.
    workersTimer = setTimeout(() => {
      if (state.view === 'projects' && state.seg === 'workers' && !state.focus) loadWorkers();
    }, 4000);
  }

  // The badge on the tab follows ticket changes, at most a few times a second.
  let workersSoon = null;
  function workersChanged() {
    if (workersSoon) return;
    workersSoon = setTimeout(() => { workersSoon = null; loadWorkers(); }, 600);
  }

  // --- settings: the person's other tools ---

  const TOOL_FORMS = {
    github: { name: 'GitHub', hint: '在 GitHub → Settings → Developer settings → Personal access tokens 建一个令牌（fine-grained，选要用的仓库和权限）。',
      fields: [['token', '令牌', 'password']] },
    email: { name: '邮箱', hint: '填邮箱地址会自动选好类型。密码处要填应用专用密码或授权码，不是登录密码。',
      fields: [['preset', '类型', 'select'], ['address', '邮箱地址', 'text'], ['password', '应用专用密码', 'password'],
        ['imap_host', '收信服务器（IMAP）', 'text', 'custom'], ['imap_port', 'IMAP 端口', 'number', 'custom'],
        ['smtp_host', '发信服务器（SMTP）', 'text', 'custom'], ['smtp_port', 'SMTP 端口', 'number', 'custom']] },
    slack: { name: 'Slack', hint: '在 api.slack.com/apps 建一个 App，给它要用的权限（chat:write、channels:read、channels:history…），装到工作区后复制 Bot Token（xoxb- 开头）。',
      fields: [['token', 'Bot Token', 'password']] },
    google: { name: 'Google（Cloud / Drive / Docs）', hint: '在 Google Cloud 控制台建一个服务账号，生成 JSON 密钥，整段粘贴到这里。要让它读写 Drive / Docs 的文件，把文件共享给服务账号的邮箱。',
      fields: [['key_json', '服务账号 JSON 密钥', 'textarea'], ['project', '默认项目（可选）', 'text']] },
    cloudflare: { name: 'Cloudflare', hint: '在 Cloudflare → My Profile → API Tokens 建一个令牌（比如“Edit zone DNS”模板，选要管理的域名）。',
      fields: [['token', 'API 令牌', 'password']] },
    godaddy: { name: 'GoDaddy', hint: '在 developer.godaddy.com/keys 建一个 Production 的 API Key。',
      fields: [['key', 'API Key', 'password'], ['secret', 'API Secret', 'password']] },
    aws: { name: 'AWS', hint: '在 AWS IAM 建一个用户（或用现有的），给它需要的权限（比如 EC2、S3、Route 53 的只读或管理），再建一个 Access Key。助理只用这把密钥，不用服务器自己的 AWS 身份。',
      fields: [['access_key_id', 'Access Key ID', 'password'], ['secret_access_key', 'Secret Access Key', 'password'],
        ['region', '默认区域', 'text']] },
  };
  const EMAIL_TYPES = [['gmail', 'Gmail'], ['outlook', 'Outlook / Microsoft 365'], ['qq', 'QQ 邮箱'],
    ['netease', '网易邮箱（163 / 126）'], ['icloud', 'iCloud'], ['custom', '其他']];
  const EMAIL_DOMAINS = {
    'gmail.com': 'gmail', 'googlemail.com': 'gmail', 'outlook.com': 'outlook', 'hotmail.com': 'outlook',
    'live.com': 'outlook', 'msn.com': 'outlook', 'outlook.cn': 'outlook', 'live.cn': 'outlook', 'qq.com': 'qq',
    'foxmail.com': 'qq', 'vip.qq.com': 'qq', '163.com': 'netease', '126.com': 'netease', 'yeah.net': 'netease',
    'icloud.com': 'icloud', 'me.com': 'icloud', 'mac.com': 'icloud',
  };
  const EMAIL_HINTS = {
    gmail: 'Gmail：先开两步验证，再到 myaccount.google.com/apppasswords 生成 16 位应用专用密码。',
    outlook: '注意：outlook.com / hotmail / live 个人邮箱从 2024 年 9 月起微软已不允许密码或应用密码登录 IMAP；公司的 Microsoft 365 邮箱要管理员开启 IMAP。',
    qq: 'QQ 邮箱：网页版 设置 → 账户 → 开启 IMAP/SMTP 服务，用生成的授权码。',
    netease: '网易邮箱：网页版 设置 → POP3/SMTP/IMAP → 开启 IMAP，用生成的授权码。',
    icloud: 'iCloud：appleid.apple.com → 登录与安全 → App 专用密码。',
    custom: '填你的邮件服务商给的 IMAP / SMTP 服务器和端口。',
  };
  // The form for one account of a tool: `target` says which (`account_id`), or that it is a new one.
  function toolForm(t, form, done, target = { new: true }) {
    const box = el('form', 'tool-form');
    box.noValidate = true;
    box.append(el('p', 'hint', form.hint));
    const grid = el('div', 'form-grid');
    const inputs = {};
    const name = el('label', 'wide');
    name.append(document.createTextNode('账号名字（比如“公司”“个人”，用来区分多个账号）'));
    inputs.label = el('input');
    inputs.label.maxLength = 40;
    inputs.label.value = target.label || '';
    inputs.label.placeholder = '不填就叫“默认”“账号 2”…';
    name.append(inputs.label);
    grid.append(name);
    for (const [key, label, type, only] of form.fields) {
      const wrap = el('label', type === 'textarea' || key === 'address' ? 'wide' : '');
      wrap.append(document.createTextNode(label));
      let input;
      if (type === 'select') {
        input = el('select');
        for (const [value, text] of EMAIL_TYPES) input.append(Object.assign(el('option', '', text), { value }));
        input.value = (t.settings && t.settings.preset) || 'gmail';
        input.addEventListener('change', () => showPreset());
      } else if (type === 'textarea') {
        input = el('textarea');
        input.rows = 5;
        input.spellcheck = false;
      } else {
        input = el('input');
        input.type = type;
        input.spellcheck = false;
        input.autocomplete = 'off';
      }
      if (type === 'password' || type === 'textarea') {
        input.placeholder = t.connected ? '已保存（留空不改）' : '';
      } else if (type !== 'select' && t.settings && t.settings[key] !== undefined) {
        input.value = t.settings[key];
      }
      if (only) wrap.dataset.only = only;
      wrap.append(input);
      inputs[key] = input;
      grid.append(wrap);
    }
    const presetHint = el('p', 'hint wide');
    function showPreset() {
      if (!inputs.preset) return;
      for (const node of grid.querySelectorAll('[data-only=custom]')) node.hidden = inputs.preset.value !== 'custom';
      const words = EMAIL_HINTS[inputs.preset.value] || '';
      presetHint.textContent = words;
      presetHint.hidden = !words;
    }
    if (inputs.preset) {
      inputs.preset.parentNode.after(presetHint);
      // A well-known address says which provider it is.
      inputs.address.addEventListener('input', () => {
        const found = EMAIL_DOMAINS[inputs.address.value.split('@')[1]?.trim().toLowerCase()];
        if (found && inputs.preset.value !== found) {
          inputs.preset.value = found;
          showPreset();
        }
      });
      showPreset();
    }
    const error = el('p', 'error');
    error.hidden = true;
    const actions = el('div', 'actions');
    const cancel = el('button', 'outline', '取消');
    cancel.type = 'button';
    cancel.addEventListener('click', () => done(null));
    const save = el('button', 'outline amber', '保存并测试');
    save.type = 'submit';
    actions.append(cancel, save);
    box.append(grid, error, actions);
    box.addEventListener('submit', async (event) => {
      event.preventDefault();
      const fields = target.account_id ? { account_id: target.account_id } : { new: true };
      for (const [key, input] of Object.entries(inputs)) if (input.value !== '') fields[key] = input.value;
      save.disabled = true;
      save.textContent = '测试中…';
      try {
        const updated = await call('toolSave', t.kind, fields);
        const saved = (updated.accounts || []).find((a) => a.id === updated.saved) || updated;
        if (!(saved.test || {}).ok) throw new Error((saved.test || {}).message || '没连上');
        done(updated);
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
        save.disabled = false;
        save.textContent = '保存并测试';
      }
    });
    return box;
  }

  // --- the tools page: every tool on the left, the chosen one on the right, like an editor's extensions ---

  const CATALOG = [
    { id: 'ssh', group: '服务器', name: 'SSH 服务器', abbr: 'SSH', color: '#3aa9ac',
      desc: '助理在你登记的服务器上执行命令、看服务和日志；每台服务器连通后会自动巡检。',
      can: ['列出服务器和它们的概况', '在服务器上执行命令（按每台的执行权限）', '负责人同时派多个 Worker 分别查多台机器'] },
    { id: 'claude_code', group: '编程助手', name: 'Claude Code', abbr: 'CC', color: '#d97757',
      desc: '用你自己的 Claude 账号，在项目的工作区里写代码、改代码、调试。',
      can: ['写或改较多代码', '读懂一个项目', '调试、跑测试'] },
    { id: 'codex', group: '编程助手', name: 'Codex', abbr: 'CX', color: '#10a37f',
      desc: '用你自己的 ChatGPT 账号，在项目的工作区里写代码、改代码、调试。',
      can: ['写或改较多代码', '读懂一个项目', '调试、跑测试'] },
    { id: 'github', group: '服务', name: 'GitHub', abbr: 'GH', color: '#8b949e',
      desc: '用你的令牌执行 gh 命令。', can: ['查 PR、issue、提交、Actions', '评论、开 issue（你明确要求时）'] },
    { id: 'email', group: '服务', name: '邮箱', abbr: '@', color: '#f2a33a',
      desc: '用 IMAP / SMTP 读写你的邮箱。', can: ['搜索、阅读邮件', '发邮件（你明确要求时）'] },
    { id: 'slack', group: '服务', name: 'Slack', abbr: 'SL', color: '#b05ce6',
      desc: '用机器人令牌调用 Slack Web API。', can: ['读频道和消息', '发消息（你明确要求时）'] },
    { id: 'google', group: '服务', name: 'Google（GCP / Drive / Docs）', abbr: 'G', color: '#4285f4',
      desc: '用服务账号执行 gcloud，调用 Drive、Docs、Sheets、Cloud 的接口。',
      can: ['管 Google Cloud 的资源', '读写共享给服务账号的 Drive、Docs、Sheets'] },
    { id: 'cloudflare', group: '服务', name: 'Cloudflare', abbr: 'CF', color: '#f38020',
      desc: '用 API 令牌调用 Cloudflare 的接口。', can: ['查看域名和 DNS 记录', '改 DNS 记录（你明确要求时）'] },
    { id: 'godaddy', group: '服务', name: 'GoDaddy', abbr: 'GD', color: '#1bdbdb',
      desc: '用 API Key 调用 GoDaddy 的接口。', can: ['查看域名和 DNS 记录', '改 DNS 记录（你明确要求时）'] },
    { id: 'aws', group: '服务', name: 'AWS', abbr: 'AWS', color: '#ff9900',
      desc: '用你给的 Access Key 执行 aws 命令，不用服务器自己的 AWS 身份。',
      can: ['查 EC2、S3、Route 53 等资源', '改资源（你明确要求、且密钥有权限时）'] },
  ];
  const toolsTab = { selected: 'ssh', integrations: [], agents: [], hosts: [], admin: true, editing: null };

  async function loadToolsTab() {
    const [integrations, agents, sshPage] = await Promise.all([
      api.tools ? call('tools').catch(() => ({ admin: false, tools: [] })) : { admin: false, tools: [] },
      api.agents ? call('agents').catch(() => []) : [],
      call('ssh').catch(() => ({ admin: false, hosts: [] })),
    ]);
    toolsTab.admin = integrations.admin || sshPage.admin;
    toolsTab.integrations = integrations.tools || [];
    toolsTab.agents = agents || [];
    toolsTab.hosts = sshPage.hosts || [];
    renderToolList();
    renderToolDetail();
  }

  // Where a tool stands, in a few words, and whether it is ready.
  function toolStatus(id) {
    if (id === 'ssh') {
      const ok = toolsTab.hosts.filter((h) => (h.state || {}).ok).length;
      return { ready: ok > 0, text: toolsTab.hosts.length ? `${ok}/${toolsTab.hosts.length} 台已连通` : '还没有服务器' };
    }
    const agent = toolsTab.agents.find((a) => a.kind === id);
    if (agent || id === 'claude_code' || id === 'codex') {
      if (!agent || !agent.installed) return { ready: false, text: '服务器上没装' };
      if (!agent.signed_in) return { ready: false, text: '没登录' };
      return { ready: agent.enabled, text: agent.enabled ? '已启用' : '已停用' };
    }
    const t = toolsTab.integrations.find((x) => x.kind === id) || {};
    const accounts = t.accounts || [];
    if (!t.connected || !accounts.length) return { ready: false, text: '没有连接' };
    const broken = accounts.filter((a) => (a.test || {}).ok === false).length;
    if (broken === accounts.length) return { ready: false, text: '连不上' };
    return { ready: true, text: accounts.length > 1 ? `${accounts.length} 个账号${broken ? `，${broken} 个连不上` : ''}` : '已连接' };
  }

  // A tool's own mark: the brand's logo where there is one, a plain drawing for SSH and email, letters otherwise.
  const PLAIN_MARKS = {
    ssh: '<rect x="3" y="4" width="18" height="7" rx="1"/><rect x="3" y="13" width="18" height="7" rx="1"/><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/>',
    email: '<rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="m3.5 6 8.5 7 8.5-7"/>',
  };
  function toolIcon(entry) {
    const brand = (window.ContinuumBrands || {})[entry.id];
    const mark = el('span', `tool-icon${brand || PLAIN_MARKS[entry.id] ? ' drawn' : ''}`);
    mark.setAttribute('aria-hidden', 'true');
    mark.style.setProperty('--c', brand ? brand.color : entry.color);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    if (brand) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', brand.path);
      svg.append(path);
      svg.setAttribute('class', 'brand');
      mark.title = brand.title;
    } else if (PLAIN_MARKS[entry.id]) {
      svg.innerHTML = PLAIN_MARKS[entry.id];
      svg.setAttribute('class', 'plain');
    } else {
      mark.textContent = entry.abbr;
      return mark;
    }
    mark.append(svg);
    return mark;
  }

  function renderToolList() {
    const query = $('tool-search').value.trim().toLowerCase();
    const shown = CATALOG.filter((e) => !query || `${e.name} ${e.desc} ${e.id} ${e.can.join(" ")}`.toLowerCase().includes(query));
    const items = [];
    for (const group of ['服务器', '编程助手', '服务']) {
      const members = shown.filter((e) => e.group === group);
      if (!members.length) continue;
      items.push(el('li', 'side-head', group));
      for (const entry of members) {
        const status = toolStatus(entry.id);
        const li = el('li');
        const button = el('button', `tool-item${status.ready ? ' ready' : ''}`);
        button.dataset.tool = entry.id;
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', String(entry.id === toolsTab.selected));
        const words = el('span', 'tool-words');
        words.append(el('b', '', entry.name), el('span', 'tool-state', status.text));
        button.append(toolIcon(entry), words, el('span', `tool-dot${status.ready ? ' on' : ''}`));
        button.addEventListener('click', () => {
          toolsTab.selected = entry.id;
          toolsTab.editing = null;
          renderToolList();
          renderToolDetail();
        });
        li.append(button);
        items.push(li);
      }
    }
    if (!items.length) items.push(el('li', 'hint', '没有找到。'));
    $('tool-list').replaceChildren(...items);
  }
  $('tool-search').addEventListener('input', renderToolList);

  function renderToolDetail() {
    const entry = CATALOG.find((e) => e.id === toolsTab.selected) || CATALOG[0];
    const isSsh = entry.id === 'ssh';
    $('ssh-pane').hidden = !isSsh;
    const pane = $('tool-pane');
    const status = toolStatus(entry.id);
    const head = el('div', 'tool-head');
    const titles = el('div', 'tool-titles');
    titles.append(el('h2', '', entry.name), el('span', `tool-pill${status.ready ? ' on' : ''}`, status.text));
    head.append(toolIcon(entry), titles);
    const nodes = [head, el('p', 'tool-desc', entry.desc)];
    const can = el('ul', 'tool-can');
    can.append(...entry.can.map((c) => el('li', '', c)));
    nodes.push(el('h3', 'tool-sub', '助理能用它做'), can);
    if (!toolsTab.admin) {
      nodes.push(el('p', 'hint', '只有管理员能管理工具。'));
    } else if (isSsh) {
      loadSsh();
    } else if (entry.id === 'claude_code' || entry.id === 'codex') {
      nodes.push(...agentDetail(entry));
    } else {
      nodes.push(...integrationDetail(entry));
    }
    pane.replaceChildren(...nodes);
  }

  function agentDetail(entry) {
    const agent = toolsTab.agents.find((a) => a.kind === entry.id) || {};
    const detail = el('dl', 'detail');
    const row = (label, value) => { if (value) detail.append(el('dt', '', label), el('dd', '', value)); };
    row('服务器上', agent.installed || '没装');
    row('登录的账号', agent.signed_in ? agent.account || '已登录' : '没登录');
    row('设置里', agent.configured ? '已配置给助理' : '没配置（服务器设置里加 [executor.config.' + entry.id + ']）');
    const nodes = [el('h3', 'tool-sub', '状态'), detail];
    if (!agent.signed_in) {
      nodes.push(el('p', 'hint', entry.id === 'claude_code'
        ? '登录要在服务器上进行：运行 claude setup-token（或 claude auth login），用你的 Claude 账号确认。'
        : '登录要在服务器上进行：运行 codex login --device-auth，按提示在浏览器里确认。'));
    }
    const actions = el('div', 'actions');
    const toggle = el('button', agent.enabled ? 'outline danger' : 'outline amber', agent.enabled ? '停用' : '启用');
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      try {
        const updated = await call('agentSwitch', entry.id, !agent.enabled);
        toolsTab.agents = toolsTab.agents.map((a) => (a.kind === entry.id ? updated : a));
        renderToolList();
        renderToolDetail();
      } catch (error) {
        showStatus(error.message);
        toggle.disabled = false;
      }
    });
    actions.append(toggle);
    nodes.push(el('p', 'hint', agent.enabled ? '启用时，助理遇到较大的编程活会交给它。' : '停用后，助理不会再把活交给它。'), actions);
    return nodes;
  }

  function integrationDetail(entry) {
    const t = toolsTab.integrations.find((x) => x.kind === entry.id) || { kind: entry.id, connected: false, accounts: [] };
    const form = TOOL_FORMS[entry.id];
    const accounts = t.accounts || [];
    const nodes = [el('h3', 'tool-sub', accounts.length ? `账号 ${pad(accounts.length)}` : '连接')];
    const refresh = (updated) => {
      if (updated) toolsTab.integrations = toolsTab.integrations.map((x) => (x.kind === entry.id ? updated : x));
      toolsTab.editing = null;
      renderToolList();
      renderToolDetail();
    };
    for (const a of accounts) {
      const test = a.test || {};
      const card = el('article', `card notched host tool-account ${test.ok === false ? 'bad' : 'ok'}`);
      card.dataset.account = a.id;
      const top = el('div', 'card-top');
      top.append(el('span', 'ticket-id', a.id), el('span', 'stage', test.ok === false ? '连不上' : '已连接'));
      card.append(top, el('div', 'card-title', a.label));
      const detail = el('dl', 'detail');
      const row = (label, value, cls = '') => { if (value) detail.append(el('dt', '', label), el('dd', cls, value)); };
      row('账号', a.account);
      if (a.settings && a.settings.address) row('邮箱', a.settings.address);
      if (a.settings && a.settings.client_email) row('服务账号', a.settings.client_email);
      if (test.at) row('上次测试', `${test.ok ? '通过' : test.message}（${day(test.at)} ${time(test.at)}）`, test.ok ? '' : 'question');
      card.append(detail);
      if (toolsTab.editing === a.id) {
        card.append(toolForm({ kind: entry.id, connected: true, settings: a.settings || {} }, form, refresh,
          { account_id: a.id, label: a.label }));
      } else {
        const actions = el('div', 'actions');
        const again = el('button', 'outline', '测试');
        again.addEventListener('click', async () => {
          again.disabled = true;
          try {
            refresh(await call('toolTest', entry.id, a.id));
          } catch (error) {
            showStatus(error.message);
            again.disabled = false;
          }
        });
        const change = el('button', 'outline', '修改');
        change.addEventListener('click', () => { toolsTab.editing = a.id; renderToolDetail(); });
        const off = el('button', 'outline danger', '断开');
        off.addEventListener('click', async () => {
          if (!off.dataset.confirm) { off.dataset.confirm = '1'; off.textContent = '确认断开？'; return; }
          try {
            await call('toolRemove', entry.id, a.id);
          } catch (error) {
            showStatus(error.message);
          }
          await loadToolsTab();
        });
        actions.append(again, change, off);
        card.append(actions);
      }
      nodes.push(card);
    }
    if (!accounts.length || toolsTab.editing === 'new') {
      if (accounts.length) nodes.push(el('h3', 'tool-sub', '添加账号'));
      nodes.push(toolForm({ kind: entry.id, connected: false, settings: {} }, form, refresh, { new: true }));
    } else {
      const add = el('button', 'outline amber add-account', '＋ 添加账号');
      add.addEventListener('click', () => { toolsTab.editing = 'new'; renderToolDetail(); });
      nodes.push(add);
    }
    return nodes;
  }

  // --- settings: the SSH servers Continuum may reach ---

  const ssh = { hosts: [], approvals: {}, editing: null, key: '', line: '' };
  $('settings').addEventListener('click', () => setView(state.view === 'settings' ? 'chat' : 'settings'));

  async function loadSsh() {
    let page;
    try {
      page = await call('ssh');
    } catch (error) {
      showStatus(error.message);
      return;
    }
    $('ssh-locked').hidden = page.admin;
    $('ssh-admin').hidden = !page.admin;
    if (!page.admin) return;
    ssh.hosts = page.hosts;
    ssh.approvals = page.approvals || {};
    ssh.key = page.public_key || '';
    const limits = 'no-agent-forwarding,no-port-forwarding,no-X11-forwarding';
    ssh.line = ssh.key ? `${page.source_address ? `from="${page.source_address}",` : ''}${limits} ${ssh.key}` : '';
    $('ssh-key').textContent = ssh.key || '服务器上还没有 Continuum 的密钥（[ssh] key）。';
    $('ssh-line').textContent = ssh.line;
    $('ssh-fp').textContent = page.fingerprint || '';
    renderSsh();
  }

  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Without clipboard access, select the text so it can be copied by hand.
      const range = document.createRange();
      range.selectNodeContents(button.closest('.card').querySelector('.key'));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
    const label = button.textContent;
    button.textContent = '已复制';
    setTimeout(() => { button.textContent = label; }, 1200);
  }
  $('ssh-copy-key').addEventListener('click', (event) => copy(ssh.key, event.currentTarget));
  $('ssh-copy-line').addEventListener('click', (event) => copy(ssh.line, event.currentTarget));

  function hostCard(h) {
    const st = h.state || {};
    const kind = st.ok === true ? 'ok' : st.ok === false ? 'bad' : 'untested';
    const box = el('article', `card notched host ${kind}`);
    box.dataset.host = h.name;
    const top = el('div', 'card-top');
    const where = el('span', 'ticket-id', `${h.user}@${h.address}${h.port === 22 ? '' : `:${h.port}`}`);
    top.append(where, el('span', 'stage', { ok: '已连通', bad: '连不上', untested: '还没测试' }[kind]));
    box.append(top, el('div', 'card-title', h.name));
    const detail = el('dl', 'detail');
    const row = (label, value, cls = '') => { if (value) detail.append(el('dt', '', label), el('dd', cls, value)); };
    row('经过跳板', h.jump);
    row('执行权限', h.approval_text || ssh.approvals[h.approval]);
    row('上次测试', st.message, st.ok === false ? 'question' : 'summary');
    row('主机指纹', st.fingerprint);
    row('备注', h.note);
    box.append(detail);
    // What runs there and what is being built, from the last look over it.
    const profile = h.profile || {};
    if (profile.text || profile.error) {
      const about = el('details', 'profile');
      const head = el('summary', '', `服务器概况 · ${day(profile.updated_at)} ${time(profile.updated_at)}`);
      about.append(head, el('div', profile.ok ? 'profile-text' : 'profile-text error', profile.text || profile.error));
      box.append(about);
    } else if (st.ok) {
      box.append(el('p', 'hint', '还没有巡检过：点“巡检”看看上面跑着什么、在开发什么。'));
    }
    const foot = el('div', 'card-foot');
    if (st.tested_at) {
      const when = el('span', 'meta-item');
      when.append(icon('calendar'), el('span', '', `${day(st.tested_at)} ${time(st.tested_at)}`));
      foot.append(when);
    }
    const actions = el('div', 'actions');
    const test = el('button', 'outline', '测试连接');
    test.addEventListener('click', async () => {
      test.disabled = true;
      test.textContent = '测试中…';
      try {
        const tested = await call('sshTest', h.name);
        ssh.hosts = ssh.hosts.map((x) => (x.name === h.name ? tested : x));
        renderSsh();
      } catch (error) {
        showStatus(error.message);
        test.disabled = false;
        test.textContent = '测试连接';
      }
    });
    const look = el('button', 'outline', profile.text ? '重新巡检' : '巡检');
    look.addEventListener('click', async () => {
      look.disabled = true;
      look.textContent = '巡检中…';
      try {
        const surveyed = await call('sshProfile', h.name);
        ssh.hosts = ssh.hosts.map((x) => (x.name === h.name ? surveyed : x));
        renderSsh();
      } catch (error) {
        showStatus(error.message);
        look.disabled = false;
        look.textContent = '巡检';
      }
    });
    const edit = el('button', 'outline', '编辑');
    edit.addEventListener('click', () => openForm(h));
    const remove = el('button', 'outline danger', '删除');
    remove.addEventListener('click', async () => {
      if (!remove.dataset.confirm) {
        remove.dataset.confirm = '1';
        remove.textContent = '确认删除？';
        return;
      }
      try {
        await call('sshDelete', h.name);
        ssh.hosts = ssh.hosts.filter((x) => x.name !== h.name);
        renderSsh();
        showStatus('');
      } catch (error) {
        showStatus(error.message);
      }
    });
    actions.append(test, look, edit, remove);
    foot.append(actions);
    box.append(foot);
    return box;
  }

  function renderBulk() {
    const select = $('ssh-bulk');
    const chosen = select.value || 'auto';
    select.replaceChildren(...Object.entries(ssh.approvals).map(([value, text]) => Object.assign(el('option', '', text), { value })));
    select.value = chosen;
  }

  // One choice for every server at once, asked twice since it decides what workers may run unasked.
  $('ssh-bulk-apply').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const approval = $('ssh-bulk').value;
    if (!button.dataset.confirm) {
      button.dataset.confirm = '1';
      button.textContent = `确认改 ${ssh.hosts.length} 台？`;
      return;
    }
    delete button.dataset.confirm;
    button.disabled = true;
    let changed = 0;
    const failed = [];
    for (const h of ssh.hosts) {
      if (h.approval === approval) continue;
      try {
        const saved = await call('sshSave', h.name, {
          address: h.address, port: h.port, user: h.user, jump: h.jump || null, note: h.note || '', approval,
        });
        ssh.hosts = ssh.hosts.map((x) => (x.name === h.name ? saved : x));
        changed++;
      } catch (error) {
        failed.push(`${h.name}：${error.message}`);
      }
    }
    button.disabled = false;
    button.textContent = '应用到全部';
    $('ssh-bulk-note').textContent = `已把 ${changed} 台改为“${ssh.approvals[approval]}”。` + (failed.length ? ` 没改成：${failed.join('；')}` : '');
    $('ssh-bulk-note').hidden = false;
    renderSsh();
  });

  function renderSsh() {
    renderBulk();
    $('ssh-hosts').replaceChildren(...ssh.hosts.map(hostCard));
    $('ssh-empty').hidden = ssh.hosts.length > 0 || !$('ssh-form').hidden;
  }

  function openForm(host) {
    ssh.editing = host ? host.name : null;
    $('ssh-form-title').textContent = host ? `编辑 ${host.name}` : '添加服务器';
    $('ssh-name').value = host ? host.name : '';
    $('ssh-name').readOnly = Boolean(host);
    $('ssh-address').value = host ? host.address : '';
    $('ssh-port').value = host ? host.port : 22;
    $('ssh-user').value = host ? host.user : '';
    $('ssh-note').value = host ? host.note || '' : '';
    const jumps = ssh.hosts.filter((h) => !host || h.name !== host.name);
    $('ssh-jump').replaceChildren(el('option', '', '不用跳板，直接连'),
      ...jumps.map((h) => Object.assign(el('option', '', `${h.name}（${h.address}）`), { value: h.name })));
    $('ssh-jump').firstChild.value = '';
    $('ssh-jump').value = host && host.jump ? host.jump : '';
    $('ssh-approval').replaceChildren(...Object.entries(ssh.approvals)
      .map(([value, text]) => Object.assign(el('option', '', text), { value })));
    $('ssh-approval').value = host ? host.approval : 'ask';
    $('ssh-error').hidden = true;
    $('ssh-form').hidden = false;
    $('ssh-add').hidden = true;
    renderSsh();
    $('ssh-form').scrollIntoView({ block: 'nearest' });
    (host ? $('ssh-address') : $('ssh-name')).focus();
  }

  function closeForm() {
    $('ssh-form').hidden = true;
    $('ssh-add').hidden = false;
    ssh.editing = null;
    renderSsh();
  }

  $('ssh-add').addEventListener('click', () => openForm(null));
  $('ssh-cancel').addEventListener('click', closeForm);
  $('ssh-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('ssh-name').value.trim();
    const host = {
      address: $('ssh-address').value.trim(), port: Number($('ssh-port').value) || 22,
      user: $('ssh-user').value.trim(), jump: $('ssh-jump').value || null, approval: $('ssh-approval').value,
      note: $('ssh-note').value.trim(),
    };
    $('ssh-save').disabled = true;
    try {
      const saved = await call('sshSave', name, host);
      ssh.hosts = [...ssh.hosts.filter((x) => x.name !== name), saved].sort((a, b) => a.name.localeCompare(b.name));
      closeForm();
    } catch (error) {
      $('ssh-error').textContent = error.message;
      $('ssh-error').hidden = false;
    } finally {
      $('ssh-save').disabled = false;
    }
  });

  // --- updates: the main process downloads a new version in the background; a restart installs it ---

  function showUpdate(info) {
    const update = info && info.update;
    $('version').textContent = info ? `当前版本 ${info.current}` + (update
      ? (update.ready ? `，新版本 ${update.version} 已下载好，重启后生效。` : `，正在下载新版本 ${update.version}…`)
      : '，有新版本时会自动下载。') : '';
    $('update').hidden = !(update && update.ready);
    $('update-install').hidden = !(update && update.ready);
    if (update && update.ready) $('update-text').textContent = `新版本 ${update.version} 已下载好。`;
  }
  if (api.updateInfo) {
    call('updateInfo').then(showUpdate).catch(() => {});
    api.onUpdate((update) => call('updateInfo').then(showUpdate).catch(() => showUpdate({ current: '', update })));
    $('update-install').addEventListener('click', () => api.installUpdate());
  }

  const UPDATE_WORDS = {
    latest: (r) => `已经是最新版本（${r.current}）。`,
    available: (r) => `发现新版本 ${r.latest}，正在后台下载，下载好后底部会提示重启。`,
    dev: () => '这是开发版本，不会自动更新。',
    error: (r) => `检测失败：${r.message}`,
  };
  async function checkUpdate(shown) {
    if (!api.checkUpdate) return;
    if (shown) {
      $('check-update').disabled = true;
      $('check-update').textContent = '检测中…';
    }
    try {
      const result = await call('checkUpdate');
      if (shown) {
        $('update-result').textContent = (UPDATE_WORDS[result.status] || (() => ''))(result);
        $('update-result').hidden = false;
      }
      showUpdate(await call('updateInfo'));
    } catch (error) {
      if (shown) {
        $('update-result').textContent = `检测失败：${error.message}`;
        $('update-result').hidden = false;
      }
    } finally {
      $('check-update').disabled = false;
      $('check-update').textContent = '检测更新';
    }
  }
  $('check-update').addEventListener('click', () => checkUpdate(true));
  if (!api.checkUpdate) $('check-update').hidden = true;

  // --- live events ---

  const lingering = new Map();
  api.onEvent((event) => {
    if (event.type === 'message') {
      addMessage(event.message);
      if (['proposal', 'notice', 'reminder', 'scheduled'].includes(event.message.role)) projectsChanged();
    } else if (event.type === 'message-updated') {
      state.messages.set(event.message.id, event.message);
      renderMessages();
    } else if (event.type === 'ticket') {
      projectsChanged();
      workersChanged();
      if (event.ticket.stage !== 'working' && state.thinking.delete(event.ticket.id)) renderMessages();
    } else if (event.type === 'step') {
      const step = event.step;
      state.steps.set(`${step.ticket_id}:${step.key}`, step);
      if (visible(step.project)) renderMessages();
      if (state.view === 'projects' && state.seg === 'workers') workersChanged();
    } else if (event.type === 'thinking') {
      state.thinking.set(event.ticket_id, event.project);
      if (visible(event.project)) renderMessages();
    } else if (event.type === 'idle') {
      state.thinking.delete(event.ticket_id);
      // An answer that was being written but never arrived as a message (the turn ended some other way) goes too.
      clearTimeout(lingering.get(event.ticket_id));
      lingering.set(event.ticket_id, setTimeout(() => {
        if (state.streams.delete(event.ticket_id)) renderMessages();
      }, 15000));
      if (visible(event.project)) renderMessages();
    } else if (event.type === 'stream' && !event.text) {
      // What was being written turned out to be the worker talking as it works; it shows as a step instead.
      if (state.streams.delete(event.ticket_id) && visible(event.project)) renderMessages();
    } else if (event.type === 'stream') {
      const fresh = !state.streams.has(event.ticket_id);
      state.streams.set(event.ticket_id, { project: event.project, text: event.text });
      if (fresh) state.thinking.delete(event.ticket_id);
      if (visible(event.project)) (fresh ? renderMessages() : updateStream(event.ticket_id));
    }
  });

  api.onState(async (connection) => {
    const was = state.connection;
    state.connection = connection;
    $('dot').dataset.state = connection;
    $('dot').title = LINK[connection] || connection;
    $('link-text').textContent = LINK[connection] || connection;
    if (connection === 'unauthorized') {
      showSetup('', '登录已失效，请重新登录。');
      return;
    }
    showStatus(connection === 'offline' && !$('main').hidden ? '和服务器断开了，正在重连…' : '');
    // Back online: catch up on what was said meanwhile.
    if (connection === 'online' && was !== 'online' && !$('main').hidden) {
      await loadAll();
      renderMessages();
    }
  });

  // --- the window's own title bar (the app window has no frame) ---

  if (api.minimize && api.hide) {
    $('win-min').addEventListener('click', () => api.minimize());
    $('win-max').addEventListener('click', () => api.maximize && api.maximize());
    $('win-close').addEventListener('click', () => api.hide());
  } else {
    document.querySelector('.window-controls').hidden = true;
  }

  // --- start ---

  (async function start() {
    const config = await call('getConfig');
    state.connection = config.connection;
    $('dot').dataset.state = config.connection;
    $('link-text').textContent = LINK[config.connection] || '';
    $('server').value = config.server || '';
    if (config.configured) await showMain(config.user);
    else showSetup(config.server);
    $('app').dataset.ready = '1';
  })().catch((error) => {
    showSetup('', error.message);
    $('app').dataset.ready = '1';
  });
})();
