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
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    repeat: '<path d="M4 12a8 8 0 0 1 14-5.3L20 9"/><path d="M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15"/><path d="M4 20v-5h5"/>',
    claude_code: '<path d="M4 12h11M11 7l5 5-5 5"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
    ssh: '<rect x="3" y="4" width="18" height="7" rx="1"/><rect x="3" y="13" width="18" height="7" rx="1"/><path d="M7 7.5h.01M7 16.5h.01"/>',
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
    $('settings').setAttribute('aria-pressed', String(view === 'settings'));
    if (view === 'projects') showProjectsTab();
    if (view === 'chat') {
      renderMessages(true);
      $('input').focus();
    }
    if (view === 'settings') loadSsh();
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

  function message(m) {
    if (m.role === 'proposal') return proposal(m);
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
    item.append(face, bubble, el('div', 'meta', time(m.at)));
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
    claude_code: '交给 Claude Code', codex: '交给 Codex', ssh: '远程命令',
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
    summary.append(icon(STEP_NAMES[step.name] ? step.name : 'think'));
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

  function workBox(ticket, steps, thinking) {
    const asking = steps.some((s) => s.status === 'approval');
    const running = thinking || asking || steps.some((s) => s.status === 'running');
    const item = el('li', `work${running ? ' running' : ''}`);
    item.dataset.ticket = ticket;
    const details = remember(el('details', 'work-box'), `w:${ticket}`, running);
    if (running) details.open = state.open.get(`w:${ticket}`) !== false;
    if (asking) details.open = true;
    const summary = el('summary');
    const counted = steps.filter((s) => s.name !== 'note' && s.name !== 'think').length;
    const current = [...steps].reverse().find((s) => s.status === 'running');
    let label;
    const busy = { claude_code: 'Claude Code 正在处理…', codex: 'Codex 正在处理…' };
    if (asking) label = '等你批准一条远程命令';
    else if (current) label = busy[current.name] || `正在${STEP_NAMES[current.name] || '处理'}…`;
    else if (thinking) label = '思考中…';
    else label = counted ? `已执行 ${counted} 个步骤` : '已思考';
    summary.append(el('span', running ? 'work-spin' : 'work-done'), el('span', 'work-label', label), icon('chevron'));
    details.append(summary);
    const list = el('ol', 'work-steps');
    list.append(...steps.map(stepItem));
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
      nodes.push(workBox(run.ticket, run.steps, thinkingHere.has(run.ticket) && run.last));
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
        nodes.push(workBox(ticket, [], true));
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
    $('worker-tools').replaceChildren(...Object.keys(TOOL_NAMES).map((k) => tile(tools[k] || 0, TOOL_NAMES[k], 'small')));
    const box = $('workers');
    box.replaceChildren();
    const group = (name, label, kind, items, live) => {
      const row = el('div', `group-row ${kind}`);
      row.append(el('h3', 'group', `${name} ${pad(items.length)}`), en(label));
      box.append(row, ...(items.length ? items.map((w) => workerCard(w, live)) : [el('p', 'hint', '没有。')]));
    };
    group('运行中', 'RUNNING', 'working', page.running, true);
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
    actions.append(test, edit, remove);
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
