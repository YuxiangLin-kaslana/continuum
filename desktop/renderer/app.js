// The window: log in once, then conversations (a channel list and the current one) and the tickets they started.
// Everything goes through window.continuum (preload.js); results come back as { ok, data } or { ok: false, error }.
(function () {
  'use strict';
  const api = window.continuum;
  const $ = (id) => document.getElementById(id);
  const state = {
    user: '', connection: 'offline', view: 'chat', thread: 'main',
    threads: [], messages: new Map(), tickets: new Map(), unread: new Set(),
    // What the workers do, per conversation: their steps (kept by the server), and what is live only now: the
    // answer being written and the model thinking, per ticket.
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
    claude_code: '<path d="M4 12h11M11 7l5 5-5 5"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
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

  // --- a small, safe Markdown: everything is escaped first; only lists, tables, headings, bold and code are read ---

  function escape(text) {
    return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function inline(text) {
    return escape(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
  }
  const ROW = /^\s*\|.*\|\s*$/;
  const RULE = /^\s*\|[\s:|-]+\|\s*$/;
  const ITEM = /^\s*([-*•]|\d+[.)])\s+/;
  function markdown(text) {
    const lines = text.replace(/\r/g, '').split('\n');
    const out = [];
    let rich = false;
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (ROW.test(line) && RULE.test(lines[i + 1] || '')) {
        const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        const head = cells(line);
        const rows = [];
        i += 2;
        while (i < lines.length && ROW.test(lines[i])) rows.push(cells(lines[i++]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
        rich = true;
      } else if (ITEM.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const items = [];
        while (i < lines.length && ITEM.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(ITEM, ''))}</li>`);
        out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`);
        rich = true;
      } else {
        if (/^#{1,4}\s+/.test(line)) {
          out.push(`<h4>${inline(line.replace(/^#{1,4}\s+/, ''))}</h4>`);
          rich = true;
        } else if (line.trim()) {
          out.push(`<p>${inline(line)}</p>`);
        }
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
    state.tickets.clear();
    state.shown.clear();
    state.unread.clear();
    state.steps.clear();
    state.streams.clear();
    state.thinking.clear();
    state.thread = 'main';
    showSetup();
  });

  // --- main ---

  async function showMain(user) {
    state.user = user;
    $('user').textContent = user;
    $('me-avatar').innerHTML = picture(user, false);
    $('setup').hidden = true;
    $('main').hidden = false;
    await Promise.all([loadThreads(), loadTickets()]);
    await openThread(state.thread);
    $('input').focus();
  }

  function setView(view) {
    state.view = view;
    for (const button of document.querySelectorAll('.tabs button')) {
      button.setAttribute('aria-selected', String(button.dataset.view === view));
    }
    $('view-chat').hidden = view !== 'chat';
    $('view-tickets').hidden = view !== 'tickets';
    if (view === 'chat') $('input').focus();
  }
  for (const button of document.querySelectorAll('.tabs button')) {
    button.addEventListener('click', () => setView(button.dataset.view));
  }

  // --- channels ---

  function threadName(name) {
    return name === 'main' ? '主对话' : name;
  }

  async function loadThreads() {
    try {
      state.threads = await call('threads');
    } catch (error) {
      showStatus(error.message);
      state.threads = [];
    }
    renderThreads();
  }

  function threadNames() {
    // The main conversation first, then the others as the server lists them (newest first).
    const names = state.threads.map((t) => t.thread).filter((n) => n !== 'main');
    if (state.thread !== 'main' && !names.includes(state.thread)) names.unshift(state.thread);
    return ['main', ...names];
  }

  function renderThreads() {
    const names = threadNames();
    $('threads').replaceChildren(...names.map((name) => {
      const item = el('li');
      const button = el('button', state.unread.has(name) ? 'unread' : '');
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(name === state.thread));
      button.dataset.thread = name;
      button.append(icon(name === 'main' ? 'chat' : 'hash'), el('span', '', threadName(name)));
      button.addEventListener('click', () => openThread(name));
      item.append(button);
      return item;
    }));
    $('thread-title').textContent = threadName(state.thread);
    $('thread-no').textContent = `#${String(names.indexOf(state.thread) + 1).padStart(3, '0')}`;
  }

  async function openThread(name) {
    state.thread = name;
    state.unread.delete(name);
    renderThreads();
    await loadHistory(name);
  }

  $('new-thread').addEventListener('click', () => {
    const d = new Date();
    const name = `t-${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
    if (!state.messages.has(name)) state.messages.set(name, []);
    state.thread = name;
    setView('chat');
    renderThreads();
    renderMessages();
  });

  // --- conversation ---

  async function loadHistory(thread) {
    const known = state.messages.get(thread) || [];
    const after = known.length ? known[known.length - 1].id : 0;
    try {
      const [rows, steps] = await Promise.all([call('history', thread, after), api.steps ? call('steps', thread) : []]);
      for (const row of rows) addMessage(row, false);
      state.steps.set(thread, new Map(steps.map((step) => [`${step.ticket_id}:${step.key}`, step])));
    } catch (error) {
      showStatus(error.message);
    }
    if (!state.messages.has(thread)) state.messages.set(thread, []);
    if (thread === state.thread) renderMessages(true);
  }

  function addMessage(row, render = true) {
    const list = state.messages.get(row.thread) || [];
    if (list.some((m) => m.id === row.id)) return;
    list.push(row);
    list.sort((a, b) => a.id - b.id);
    state.messages.set(row.thread, list);
    // The answer arrived: what was being written gives way to it.
    if (row.ticket_id && row.role !== 'user' && row.role !== 'acknowledgement') state.streams.delete(row.ticket_id);
    if (!state.threads.some((t) => t.thread === row.thread)) {
      state.threads.unshift({ thread: row.thread });
      renderThreads();
    }
    if (row.thread !== state.thread) {
      if (row.role !== 'user') state.unread.add(row.thread);
      renderThreads();
    } else if (render) {
      renderMessages();
    }
  }

  function message(m) {
    const item = el('li', `msg ${m.role}`);
    const mine = m.role === 'user';
    const face = mine ? avatar(m.user || state.user, false) : avatar(ASSISTANT, true);
    const bubble = el('div', 'bubble');
    if (m.role === 'reply') {
      const { html, rich } = markdown(m.text);
      bubble.innerHTML = html;
      if (rich) item.classList.add('rich');
    } else {
      bubble.textContent = m.text;
    }
    item.append(face, bubble, el('div', 'meta', time(m.at)));
    return arrival(item, `m:${m.id}`);
  }

  // --- what the worker does: its steps, folded into one box per ticket, the way a chat app shows tool calls ---

  const STEP_NAMES = {
    shell: '运行命令', read_file: '读取文件', write_file: '写入文件', list_files: '查看目录',
    claude_code: '交给 Claude Code', codex: '交给 Codex',
  };

  function remember(details, key, fallback) {
    details.open = state.open.has(key) ? state.open.get(key) : fallback;
    details.addEventListener('toggle', () => state.open.set(key, details.open));
    return details;
  }

  function stepItem(step) {
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
    if (step.output) details.append(el(step.name === 'think' ? 'div' : 'pre', 'step-output', step.output));
    else if (step.status === 'running') details.append(el('div', 'step-output waiting', '等待输出…'));
    item.append(details);
    return item;
  }

  function workBox(ticket, steps, thinking) {
    const running = thinking || steps.some((s) => s.status === 'running');
    const item = el('li', `work${running ? ' running' : ''}`);
    item.dataset.ticket = ticket;
    const details = remember(el('details', 'work-box'), `w:${ticket}`, running);
    if (running) details.open = state.open.get(`w:${ticket}`) !== false;
    const summary = el('summary');
    const counted = steps.filter((s) => s.name !== 'note' && s.name !== 'think').length;
    const current = [...steps].reverse().find((s) => s.status === 'running');
    let label;
    const busy = { claude_code: 'Claude Code 正在处理…', codex: 'Codex 正在处理…' };
    if (current) label = busy[current.name] || `正在${STEP_NAMES[current.name] || '处理'}…`;
    else if (thinking) label = '思考中…';
    else label = counted ? `已执行 ${counted} 个步骤` : '已思考';
    summary.append(el('span', running ? 'work-spin' : 'work-done'), el('span', 'work-label', label), icon('chevron'));
    details.append(summary);
    const list = el('ol', 'work-steps');
    list.append(...steps.map(stepItem));
    if (thinking && !current) list.append(el('li', 'step thinking', '思考中…'));
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

  function renderMessages(toBottom = false) {
    const box = $('messages');
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const list = state.messages.get(state.thread) || [];
    const steps = [...(state.steps.get(state.thread) || new Map()).values()];
    // Messages and steps in the order they happened; the steps of one ticket in a row share a box.
    const timeline = [...list.map((m) => ({ at: m.at, m })), ...steps.map((s) => ({ at: s.at, s }))]
      .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    const nodes = [];
    const thinkingHere = new Set([...state.thinking].filter(([, thread]) => thread === state.thread).map(([t]) => t));
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
      if (live.thread === state.thread) nodes.push(streamItem(ticket, live.text));
    }
    box.replaceChildren(...nodes);
    $('empty-chat').hidden = nodes.length > 0;
    if (toBottom || near) box.scrollTop = box.scrollHeight;
  }

  // The answer as it is written: only its bubble changes, so the rest of the conversation stays as it is.
  function updateStream(ticket) {
    const live = state.streams.get(ticket);
    const node = document.getElementById(`stream-${ticket}`);
    if (!live || !node || live.thread !== state.thread) {
      if (live && live.thread === state.thread) renderMessages();
      return;
    }
    const box = $('messages');
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const { html, rich } = markdown(live.text);
    node.querySelector('.bubble').innerHTML = html;
    node.classList.toggle('rich', rich);
    if (near) box.scrollTop = box.scrollHeight;
  }

  $('composer').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = $('input').value.trim();
    if (!text) return;
    $('send').disabled = true;
    try {
      const row = await call('post', state.thread, text);
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

  // --- tickets ---

  async function loadTickets() {
    try {
      const list = await call('tickets');
      state.tickets = new Map(list.map((t) => [t.id, t]));
    } catch (error) {
      showStatus(error.message);
    }
    renderTickets();
  }

  function stageText(t) {
    if (t.stage === 'ended') return OUTCOMES[t.outcome] || t.outcome;
    return STAGES[t.action ? `${t.stage}/${t.action}` : t.stage] || STAGES[t.stage] || t.stage;
  }

  function card(t) {
    const box = el('article', `card notched ${t.stage}`);
    if (t.stage === 'ended' && t.outcome === 'completed') {
      const check = el('span', 'check');
      check.append(icon('check'));
      box.append(check);
    }
    const top = el('div', 'card-top');
    const id = el('span', 'ticket-id', `#${t.id.replace(/[^A-Za-z0-9]/g, '').slice(-4).toUpperCase()}`);
    id.setAttribute('aria-hidden', 'true');
    top.append(id, el('span', 'stage', stageText(t)));
    box.append(top, el('div', 'card-title', t.title));

    const rows = [];
    if (t.question) rows.push(['需要你回答', t.question, 'question']);
    if (t.report && t.report.summary) rows.push(['最新进展', t.report.summary, 'summary']);
    if (rows.length) {
      const detail = el('dl', 'detail');
      for (const [label, value, cls] of rows) detail.append(el('dt', '', label), el('dd', cls, value));
      box.append(detail);
    }

    const foot = el('div', 'card-foot');
    const when = el('span', 'meta-item');
    when.append(icon('calendar'), el('span', '', day(t.updated_at)));
    foot.append(when);
    if (t.thread) {
      const where = el('span', 'meta-item');
      where.append(icon('tag'), el('span', '', threadName(t.thread)));
      foot.append(where);
    }
    const actions = el('div', 'actions');
    if (t.thread) {
      const open = el('button', t.question ? 'outline amber' : 'outline', t.question ? '去回答' : '查看详情');
      open.addEventListener('click', async () => {
        setView('chat');
        await openThread(t.thread);
      });
      actions.append(open);
    }
    if (t.can_retry) {
      const retry = el('button', 'outline', '重试');
      retry.addEventListener('click', () => act('retry', t.id));
      actions.append(retry);
    }
    if (t.stage !== 'ended') {
      const stop = el('button', 'outline danger', '停止');
      stop.addEventListener('click', () => {
        if (stop.dataset.confirm) act('stop', t.id);
        else {
          stop.dataset.confirm = '1';
          stop.textContent = '确认停止？';
        }
      });
      actions.append(stop);
    }
    foot.append(actions);
    box.append(foot);
    return arrival(box, `t:${t.id}:${t.stage}:${t.outcome || ''}`);
  }

  async function act(method, id) {
    try {
      const updated = await call(method, id);
      state.tickets.set(updated.id, updated);
      renderTickets();
      showStatus('');
    } catch (error) {
      showStatus(error.message);
    }
  }

  function renderTickets() {
    const all = [...state.tickets.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const groups = [
      ['等你处理', 'NEEDS YOU', 'person', all.filter((t) => t.stage === 'person')],
      ['执行中', 'IN PROGRESS', 'working', all.filter((t) => t.stage === 'working' || t.stage === 'idle')],
      ['已结束', 'ENDED', 'ended', all.filter((t) => t.stage === 'ended').slice(0, 10)],
    ];
    const box = $('tickets');
    box.replaceChildren();
    for (const [name, label, kind, items] of groups) {
      if (!items.length) continue;
      const row = el('div', `group-row ${kind}`);
      row.append(el('h3', 'group', `${name} ${pad(items.length)}`), en(label));
      box.append(row, ...items.map(card));
    }
    $('empty-tickets').hidden = all.length > 0;
    const waiting = groups[0][3].length;
    $('badge').hidden = waiting === 0;
    $('badge').textContent = String(waiting);
  }

  // --- live events ---

  const lingering = new Map();
  api.onEvent((event) => {
    if (event.type === 'message') addMessage(event.message);
    else if (event.type === 'ticket') {
      state.tickets.set(event.ticket.id, event.ticket);
      renderTickets();
      if (event.ticket.stage !== 'working' && state.thinking.delete(event.ticket.id)) renderMessages();
    } else if (event.type === 'step') {
      const step = event.step;
      if (!state.steps.has(step.thread)) state.steps.set(step.thread, new Map());
      state.steps.get(step.thread).set(`${step.ticket_id}:${step.key}`, step);
      if (step.thread === state.thread) renderMessages();
    } else if (event.type === 'thinking') {
      state.thinking.set(event.ticket_id, event.thread);
      if (event.thread === state.thread) renderMessages();
    } else if (event.type === 'idle') {
      state.thinking.delete(event.ticket_id);
      // An answer that was being written but never arrived as a message (the turn ended some other way) goes too.
      clearTimeout(lingering.get(event.ticket_id));
      lingering.set(event.ticket_id, setTimeout(() => {
        if (state.streams.delete(event.ticket_id)) renderMessages();
      }, 15000));
      if (event.thread === state.thread) renderMessages();
    } else if (event.type === 'stream' && !event.text) {
      // What was being written turned out to be the worker talking as it works; it shows as a step instead.
      if (state.streams.delete(event.ticket_id) && event.thread === state.thread) renderMessages();
    } else if (event.type === 'stream') {
      const fresh = !state.streams.has(event.ticket_id);
      state.streams.set(event.ticket_id, { thread: event.thread, text: event.text });
      if (fresh) state.thinking.delete(event.ticket_id);
      if (event.thread === state.thread) (fresh ? renderMessages() : updateStream(event.ticket_id));
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
      await Promise.all([loadHistory(state.thread), loadTickets()]);
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
