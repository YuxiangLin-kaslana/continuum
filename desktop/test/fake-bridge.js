// A stand-in for preload.js, for testing the window in a plain browser: the same calls, backed by memory.
(function () {
  const listeners = { event: [], state: [] };
  const fire = (kind, payload) => listeners[kind].forEach((fn) => fn(payload));
  const now = () => new Date().toISOString();
  let configured = false;
  let nextId = 1;
  const messages = [];
  const tickets = [
    { id: 't-1', title: '核对 Laser Control Board 的供应商报价', thread: 'main', stage: 'person', action: 'answer',
      outcome: null, question: 'R12 用 0402 还是 0603 封装？两家报价差 12%。', can_retry: false,
      report: { state: 'progress', summary: '已比对 3 家报价，发现 4 处差异。' }, updated_at: '2026-11-03T10:02:00Z' },
    { id: 't-2', title: '整理本周交期变化', thread: 'main', stage: 'working', action: null, outcome: null,
      question: null, can_retry: false, report: null, updated_at: '2026-11-03T10:01:00Z' },
    { id: 't-3', title: '更新 BOM v3 的替代料', thread: 'main', stage: 'ended', action: null, outcome: 'completed',
      question: null, can_retry: false, report: { state: 'ready', summary: '已生成 BOM_v3_alt.xlsx' },
      updated_at: '2026-11-03T09:00:00Z' },
  ];
  const ok = (data) => Promise.resolve({ ok: true, data });
  const add = (thread, role, text) => {
    const row = { id: nextId++, user: 'me', thread, role, text, at: now() };
    messages.push(row);
    return row;
  };
  add('main', 'user', '帮我核对一下 Laser Control Board 的供应商报价');
  add('main', 'acknowledgement', '收到，开始处理：核对 Laser Control Board 的供应商报价');
  add('bom', 'user', '列一下 R12 的两家报价');
  add('bom', 'reply', '#### R12 报价对比\n| 供应商 | 封装 | 单价 |\n|---|---|---|\n| 甲 | 0402 | **0.012** |\n| 乙 | 0603 | 0.010 |\n- 差价 12%\n- <b>未</b>含运费');

  window.__fake = { fire, messages, tickets };
  window.continuum = {
    getConfig: () => ok({ server: 'https://continuum.example.com', user: configured ? 'me' : '', configured,
      connection: configured ? 'online' : 'offline' }),
    saveConfig: ({ username, password }) => {
      if (username !== 'me' || password !== 'good-password') {
        return Promise.resolve({ ok: false, error: '用户名或密码不对', status: 401 });
      }
      configured = true;
      setTimeout(() => fire('state', 'online'), 10);
      return ok({ user: 'me' });
    },
    logout: () => { configured = false; return ok(true); },
    threads: () => ok([...new Set(messages.map((m) => m.thread).reverse())].map((thread) => ({ thread }))),
    history: (thread, after) => ok(messages.filter((m) => m.thread === thread && m.id > after)),
    post: (thread, text) => {
      const row = add(thread, 'user', text);
      setTimeout(() => {
        fire('event', { type: 'message', message: add(thread, 'acknowledgement', `收到，开始处理：${text}`) });
        fire('event', { type: 'message', message: add(thread, 'reply', `已处理「${text}」`) });
      }, 30);
      return ok(row);
    },
    tickets: () => ok(tickets),
    stop: (id) => {
      const t = tickets.find((x) => x.id === id);
      Object.assign(t, { stage: 'ended', action: null, outcome: 'stopped', question: null });
      return ok(t);
    },
    retry: (id) => ok(tickets.find((x) => x.id === id)),
    minimize: () => {},
    hide: () => {},
    maximize: () => {},
    onEvent: (fn) => listeners.event.push(fn),
    onState: (fn) => listeners.state.push(fn),
  };
})();
