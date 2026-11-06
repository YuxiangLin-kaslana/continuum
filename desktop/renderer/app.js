// The window: connect once, then a conversation and the tickets it started. Everything goes through
// window.continuum (preload.js); results come back as { ok, data } or { ok: false, error }.
(function () {
  'use strict';
  const api = window.continuum;
  const $ = (id) => document.getElementById(id);
  const state = {
    user: '', connection: 'offline', view: 'chat', thread: 'main',
    threads: [], messages: new Map(), tickets: new Map(), unread: new Set(),
    // What has been on screen already: only what is new plays its entrance.
    shown: new Set(),
  };

  function arrival(node, key) {
    if (!state.shown.has(key)) {
      state.shown.add(key);
      node.classList.add('arrived');
    }
    return node;
  }

  const STAGES = {
    'person/answer': '等你回答', 'person/review': '等你看结果', 'person/blocked': '卡住了',
    'person/failed': '失败了', 'person/delivery': '消息没送到', 'working/queued': '排队中',
    working: '进行中', idle: '等待中',
  };
  const OUTCOMES = { completed: '已完成', stopped: '已停止', dropped: '无需处理', merged: '已合并', closed: '已关闭' };
  // The HUD's own words beside the Chinese ones; screen readers skip them.
  const STAGES_EN = {
    'person/answer': 'AWAITING ORDERS', 'person/review': 'DEBRIEF READY', 'person/blocked': 'BLOCKED',
    'person/failed': 'MISSION FAILED', 'person/delivery': 'COMMS FAILURE', 'working/queued': 'STANDBY',
    working: 'IN PROGRESS', idle: 'HOLDING',
  };
  const OUTCOMES_EN = { completed: 'MISSION COMPLETE', stopped: 'ABORTED', dropped: 'STOOD DOWN', merged: 'MERGED', closed: 'CLOSED' };
  const LINK = { online: 'LINK ESTABLISHED', connecting: 'ACQUIRING LINK…', offline: 'LINK LOST', unauthorized: 'AUTH REJECTED' };

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

  // An instrument label: decoration for the eye, hidden from screen readers.
  function en(text) {
    const node = el('span', 'en', text);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function time(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function showStatus(text) {
    $('status').textContent = text || '';
    $('status').hidden = !text;
  }

  // --- setup ---

  function showSetup(server, error) {
    $('main').hidden = true;
    $('setup').hidden = false;
    $('server').value = server || $('server').value;
    $('setup-error').hidden = !error;
    $('setup-error').textContent = error || '';
    ($('server').value ? $('token') : $('server')).focus();
  }

  $('setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('connect').disabled = true;
    $('setup-error').hidden = true;
    try {
      const saved = await call('saveConfig', { server: $('server').value, token: $('token').value.trim() });
      $('token').value = '';
      await showMain(saved.user);
    } catch (error) {
      $('setup-error').textContent = error.status === 401 ? '令牌无效或已被收回。' : error.message;
      $('setup-error').hidden = false;
    } finally {
      $('connect').disabled = false;
    }
  });

  $('menu').addEventListener('click', async () => {
    await call('logout');
    state.messages.clear();
    state.tickets.clear();
    showSetup();
  });

  // --- main ---

  async function showMain(user) {
    state.user = user;
    $('user').textContent = user;
    $('setup').hidden = true;
    $('main').hidden = false;
    await Promise.all([loadThreads(), loadTickets()]);
    await loadHistory(state.thread);
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

  // --- conversations ---

  async function loadThreads() {
    try {
      state.threads = await call('threads');
    } catch (error) {
      showStatus(error.message);
      state.threads = [];
    }
    renderThreads();
  }

  function renderThreads() {
    const names = state.threads.map((t) => t.thread);
    if (!names.includes(state.thread)) names.unshift(state.thread);
    const select = $('thread');
    select.replaceChildren(...names.map((name) => {
      const option = el('option', '', (state.unread.has(name) ? '● ' : '') + (name === 'main' ? '主对话' : name));
      option.value = name;
      option.selected = name === state.thread;
      return option;
    }));
  }

  $('thread').addEventListener('change', async () => {
    state.thread = $('thread').value;
    state.unread.delete(state.thread);
    renderThreads();
    await loadHistory(state.thread);
  });

  $('new-thread').addEventListener('click', () => {
    const stamp = new Date();
    const name = `t-${stamp.getMonth() + 1}${String(stamp.getDate()).padStart(2, '0')}-` +
      `${String(stamp.getHours()).padStart(2, '0')}${String(stamp.getMinutes()).padStart(2, '0')}`;
    state.thread = name;
    state.messages.set(name, []);
    renderThreads();
    renderMessages();
    $('input').focus();
  });

  async function loadHistory(thread) {
    const known = state.messages.get(thread) || [];
    const after = known.length ? known[known.length - 1].id : 0;
    try {
      const rows = await call('history', thread, after);
      for (const row of rows) addMessage(row, false);
    } catch (error) {
      showStatus(error.message);
    }
    if (!state.messages.has(thread)) state.messages.set(thread, []);
    renderMessages();
  }

  function addMessage(row, render = true) {
    const list = state.messages.get(row.thread) || [];
    if (list.some((m) => m.id === row.id)) return;
    list.push(row);
    list.sort((a, b) => a.id - b.id);
    state.messages.set(row.thread, list);
    if (row.thread !== state.thread) {
      state.unread.add(row.thread);
      if (!state.threads.some((t) => t.thread === row.thread)) state.threads.unshift({ thread: row.thread });
      renderThreads();
    } else if (render) {
      renderMessages();
    }
  }

  function renderMessages() {
    const list = state.messages.get(state.thread) || [];
    const box = $('messages');
    box.replaceChildren(...list.map((m) => {
      const item = el('li', `msg ${m.role}`);
      if (m.role === 'user' || m.role === 'reply') {
        const tag = el('div', 'tag', m.role === 'user' ? (state.user || 'YOU').toUpperCase() : 'CONTINUUM');
        tag.setAttribute('aria-hidden', 'true');
        item.append(tag);
      }
      item.append(el('div', 'text', m.text));
      if (m.role !== 'acknowledgement') item.append(el('div', 'meta', time(m.at)));
      return arrival(item, `m:${m.id}`);
    }));
    $('empty-chat').hidden = list.length > 0;
    box.scrollTop = box.scrollHeight;
  }

  $('composer').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = $('input').value.trim();
    if (!text) return;
    $('send').disabled = true;
    try {
      const row = await call('post', state.thread, text);
      $('input').value = '';
      addMessage(row);
      showStatus('');
    } catch (error) {
      showStatus(`没发出去：${error.message}`);
    } finally {
      $('send').disabled = false;
      $('input').focus();
    }
  });

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

  function stageKey(t) {
    return t.action ? `${t.stage}/${t.action}` : t.stage;
  }

  function stageText(t) {
    if (t.stage === 'ended') return OUTCOMES[t.outcome] || t.outcome;
    return STAGES[stageKey(t)] || t.stage;
  }

  function stageEn(t) {
    if (t.stage === 'ended') return OUTCOMES_EN[t.outcome] || String(t.outcome || '').toUpperCase();
    return STAGES_EN[stageKey(t)] || t.stage.toUpperCase();
  }

  function card(t) {
    const box = el('article', `card frame ${t.stage}`);
    const head = el('div', 'card-head');
    const id = el('span', 'mission-id', `MSN-${t.id.slice(0, 4).toUpperCase()}`);
    id.setAttribute('aria-hidden', 'true');
    head.append(id);
    const pill = el('span', `pill ${t.stage} ${t.action || ''} ${t.outcome || ''}`.trim());
    pill.append(el('span', '', stageText(t)), en(stageEn(t)));
    head.append(pill);
    head.append(el('div', 'card-title', t.title));
    box.append(head);
    if (t.question) {
      const question = el('div', 'question');
      question.append(en('REQUEST FOR ORDERS'), document.createTextNode(t.question));
      box.append(question);
    }
    if (t.report && t.report.summary) {
      const summary = el('div', 'summary');
      summary.append(en('SITREP'), document.createTextNode(t.report.summary));
      box.append(summary);
    }
    const actions = el('div', 'actions');
    if (t.thread) {
      const open = el('button', 'ghost', t.question ? '去回答' : '看对话');
      open.addEventListener('click', async () => {
        state.thread = t.thread;
        renderThreads();
        setView('chat');
        await loadHistory(t.thread);
      });
      actions.append(open);
    }
    if (t.can_retry) {
      const retry = el('button', 'ghost', '重试');
      retry.addEventListener('click', () => act('retry', t.id));
      actions.append(retry);
    }
    if (t.stage !== 'ended') {
      const stop = el('button', 'ghost danger', '停止');
      stop.addEventListener('click', () => {
        if (stop.dataset.confirm) act('stop', t.id);
        else { stop.dataset.confirm = '1'; stop.textContent = '确认停止？'; }
      });
      actions.append(stop);
    }
    box.append(actions);
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
      ['等你处理', 'AWAITING ORDERS', 'person', all.filter((t) => t.stage === 'person')],
      ['进行中', 'ACTIVE OPERATIONS', 'working', all.filter((t) => t.stage === 'working' || t.stage === 'idle')],
      ['已结束', 'MISSION LOG', 'ended', all.filter((t) => t.stage === 'ended').slice(0, 10)],
    ];
    const box = $('tickets');
    box.replaceChildren();
    for (const [name, label, kind, items] of groups) {
      if (!items.length) continue;
      const row = el('div', `group-row ${kind}`);
      row.append(el('h2', 'group', `${name} · ${items.length}`), en(label));
      box.append(row, ...items.map(card));
    }
    $('empty-tickets').hidden = all.length > 0;
    const waiting = groups[0][3].length;
    $('badge').hidden = waiting === 0;
    $('badge').textContent = String(waiting);
  }

  // --- live events ---

  api.onEvent((event) => {
    if (event.type === 'message') addMessage(event.message);
    else if (event.type === 'ticket') {
      state.tickets.set(event.ticket.id, event.ticket);
      renderTickets();
    }
  });

  api.onState(async (connection) => {
    const was = state.connection;
    state.connection = connection;
    $('dot').dataset.state = connection;
    $('link-text').textContent = LINK[connection] || connection.toUpperCase();
    $('dot').title = { online: '已连接', connecting: '连接中', offline: '未连接', unauthorized: '登录已失效' }[connection] || connection;
    if (connection === 'unauthorized') {
      showSetup('', '登录已失效，请重新输入令牌。');
      return;
    }
    showStatus(connection === 'offline' ? '和服务器断开了，正在重连…' : '');
    // Back online: catch up on what was said meanwhile.
    if (connection === 'online' && was !== 'online' && !$('main').hidden) {
      await Promise.all([loadHistory(state.thread), loadTickets()]);
    }
  });

  // --- the window's own title bar (the app window has no frame) ---

  if (api.minimize && api.hide) {
    $('win-min').addEventListener('click', () => api.minimize());
    $('win-close').addEventListener('click', () => api.hide());
  } else {
    document.querySelector('.window-controls').hidden = true;
  }

  // --- start ---

  (async function start() {
    const config = await call('getConfig');
    $('dot').dataset.state = config.connection;
    $('link-text').textContent = LINK[config.connection] || '';
    if (config.configured) await showMain(config.user);
    else showSetup(config.server);
    $('app').dataset.ready = '1';
  })().catch((error) => {
    showSetup('', error.message);
    $('app').dataset.ready = '1';
  });
})();
