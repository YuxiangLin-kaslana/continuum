// A stand-in for preload.js, for testing the window in a plain browser: the same calls, backed by memory.
(function () {
  const listeners = { event: [], state: [], update: [] };
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
  const add = (thread, role, text, ticket = null) => {
    const row = { id: nextId++, user: 'me', thread, role, text, ticket_id: ticket, at: now() };
    messages.push(row);
    return row;
  };
  const APPROVALS = { ask: '每条命令都先问你', read: '只读命令直接执行，改动先问你', auto: '直接执行' };
  const sshHosts = [
    { name: 'andrew', address: '10.128.0.20', port: 22, user: 'Yuxia', jump: null, approval: 'ask', note: '',
      approval_text: APPROVALS.ask, state: {} },
  ];
  const steps = [];
  let nextStep = 1;
  const step = (thread, ticket, key, name, input, status, output = '') => {
    let row = steps.find((x) => x.ticket_id === ticket && x.key === key);
    if (!row) {
      row = { id: nextStep++, user: 'me', thread, ticket_id: ticket, key, name, input, at: now() };
      steps.push(row);
    }
    Object.assign(row, { status, output, ended_at: status === 'running' ? null : now() });
    return { ...row };
  };
  add('main', 'user', '帮我核对一下 Laser Control Board 的供应商报价');
  add('main', 'acknowledgement', '收到，开始处理：核对 Laser Control Board 的供应商报价', 't-1');
  // A conversation made to be too wide: long commands, a wide table, code, a long URL.
  add('wide', 'user', '我现在能看到多少台服务器？', 't-9');
  step('wide', 't-9', 'w1', 'shell', 'hostname; whoami; cat /etc/hosts 2>/dev/null; echo "---"; env | grep -i -E "server|host|ssh|inventory" 2>/dev/null | head -20; ls -la ~/.ssh ~/.aws ~/.kube /etc/ssh 2>&1', 'done', '[exit code 0]\n' + 'x'.repeat(400));
  step('wide', 't-9', 'w2', 'shell', 'echo "HOME=$HOME"\nls -la $HOME 2>&1 | head\necho "=== aws config"; ls -la $HOME/.aws 2>&1', 'done', '[exit code 0]\nok');
  add('wide', 'reply', [
    '#### 结果', '我这边看不到任何外部服务器，只有 1 台：**ip-172-31-14-189.eu-west-2.compute.internal**，*AWS EC2*。',
    '详见 https://ai-api.yuxianglin.xyz/api/v1/very/long/path/that/keeps/going/and/going/without/any/break/at/all/0123456789 。',
    '[文档](https://example.com/docs)', '> 这是引用，<script>alert(1)</script> 不会执行', '---',
    '| 名字 | 地址 | 说明 | 另一个很长很长的列 | 再来一列 |', '|---|---|---|---|---|',
    '| parts-catalog-deploy-and-something-long | 34.56.245.201 | 超长说明' + '文字'.repeat(30) + ' | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | b |',
    '```bash', 'for h in $(cat hosts); do ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" hostname || echo "$h unreachable"; done # ' + 'x'.repeat(120), '```',
    '1. 第一', '2. 第二',
  ].join('\n'), 't-9');
  step('main', 't-1', 'k1', 'think', '4', 'done', '先把三家的报价单读进来，按料号对齐，再比单价和封装。');
  step('main', 't-1', 'k2', 'read_file', 'quotes/vendor_a.csv', 'done', 'part,package,price\nR12,0402,0.012\n…');
  step('main', 't-1', 'k3', 'shell', 'python3 compare.py quotes/', 'done', '[exit code 0]\n4 differences found');
  add('bom', 'user', '列一下 R12 的两家报价');
  add('bom', 'reply', '#### R12 报价对比\n| 供应商 | 封装 | 单价 |\n|---|---|---|\n| 甲 | 0402 | **0.012** |\n| 乙 | 0603 | 0.010 |\n- 差价 12%\n- <b>未</b>含运费');

  // A test can hold a turn at its steps (`hold`), then let it go on one step at a time (`release()`).
  // A release that comes before the turn reaches its pause is kept, so the turn goes straight on.
  let held = null;
  let released = 0;
  window.__fake = { fire, messages, tickets, steps, hold: false, release: () => {
    if (held) held(); else released++;
    held = null;
  } };
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
      // A turn as the server streams it: acknowledged, thinking, a command run, the answer written as it comes.
      const ticket = `t-${row.id}`;
      const answer = `已处理「${text}」\n- 交期变化 **2** 处\n- 已写入 \`/work/report.md\``;
      const live = (event) => fire('event', { ...event, ticket_id: ticket, thread });
      const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      // Held, the turn stops while the command runs and again while the answer is half written, until released.
      const pause = () => {
        if (!window.__fake.hold) return Promise.resolve();
        if (released) { released--; return Promise.resolve(); }
        return new Promise((resolve) => { held = resolve; });
      };
      (async () => {
        await wait(30);
        fire('event', { type: 'message', message: add(thread, 'acknowledgement', `收到，开始处理：${text}`, ticket) });
        live({ type: 'thinking' });
        await wait(90);
        fire('event', { type: 'step', step: step(thread, ticket, 'a', 'shell', 'ls /work', 'running') });
        await wait(140);
        await pause();
        fire('event', { type: 'step',
          step: step(thread, ticket, 'a', 'shell', 'ls /work', 'done', '[exit code 0]\nbom.csv\nleadtimes.csv') });
        live({ type: 'thinking' });
        await wait(60);
        for (let n = 4; n < answer.length + 4; n += 4) {
          live({ type: 'stream', text: answer.slice(0, n) });
          await wait(25);
        }
        await pause();
        fire('event', { type: 'message', message: add(thread, 'reply', answer, ticket) });
        live({ type: 'idle' });
      })();
      return ok(row);
    },
    steps: (thread) => ok(steps.filter((x) => x.thread === thread).map((x) => ({ ...x }))),
    tickets: () => ok(tickets),
    // SSH servers, as an admin sees them; a host whose address starts with 10. times out, as from another cloud.
    ssh: () => ok({ admin: true, public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAvfSbnyut continuum@ai-api.yuxianglin.xyz',
      fingerprint: 'SHA256:PJAca3IkzE4nJ7pqzN49pMQAw37wQF02BDE1ofh2Hjc', hosts: sshHosts.map((h) => ({ ...h })),
      approvals: APPROVALS, source_address: '13.134.76.110' }),
    sshSave: (name, host) => {
      if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(name)) return Promise.resolve({ ok: false, status: 422, error: '名字只能用小写字母、数字和 -，最长 40 个字符' });
      if (host.jump && !sshHosts.some((h) => h.name === host.jump)) return Promise.resolve({ ok: false, status: 422, error: `没有叫 ${host.jump} 的跳板` });
      const row = { name, port: 22, approval: 'ask', jump: null, note: '', ...host, state: {} };
      row.approval_text = APPROVALS[row.approval];
      const at = sshHosts.findIndex((h) => h.name === name);
      if (at >= 0) sshHosts[at] = row; else sshHosts.push(row);
      sshHosts.sort((a, b) => a.name.localeCompare(b.name));
      return ok({ ...row });
    },
    sshDelete: (name) => {
      if (sshHosts.some((h) => h.jump === name)) return Promise.resolve({ ok: false, status: 409, error: `${sshHosts.filter((h) => h.jump === name).map((h) => h.name).join(', ')} 还经过它跳转，先改掉它们` });
      sshHosts.splice(sshHosts.findIndex((h) => h.name === name), 1);
      return ok({ deleted: name });
    },
    sshTest: (name) => new Promise((resolve) => setTimeout(() => {
      const h = sshHosts.find((x) => x.name === name);
      const direct = h.address.startsWith('10.') && !h.jump;
      h.state = direct
        ? { ok: false, tested_at: now(), message: '连接超时：地址从这台服务器到不了（内网地址、防火墙没放行、或者机器没开）', fingerprint: null }
        : { ok: true, tested_at: now(), message: `${name}-box`, fingerprint: 'SHA256:q3Xc9y0w1pZcF8kT2b1A0Tn9d6Q (ED25519)' };
      resolve({ ok: true, data: { ...h, seconds: 0.4 } });
    }, 150)),
    stop: (id) => {
      const t = tickets.find((x) => x.id === id);
      Object.assign(t, { stage: 'ended', action: null, outcome: 'stopped', question: null });
      return ok(t);
    },
    retry: (id) => ok(tickets.find((x) => x.id === id)),
    minimize: () => {},
    hide: () => {},
    maximize: () => {},
    updateInfo: () => ok({ current: '0.0.6', update: window.__fake.update || null }),
    installUpdate: () => { window.__fake.installed = true; },
    checkUpdate: () => {
      window.__fake.checks = (window.__fake.checks || 0) + 1;
      return ok(window.__fake.latest
        ? { status: 'available', current: '0.0.6', latest: window.__fake.latest }
        : { status: 'latest', current: '0.0.6', latest: '0.0.6' });
    },
    onUpdate: (fn) => listeners.update.push(fn),
    onEvent: (fn) => listeners.event.push(fn),
    onState: (fn) => listeners.state.push(fn),
  };
})();
