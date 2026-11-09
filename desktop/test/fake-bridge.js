// A stand-in for preload.js, for testing the window in a plain browser: the same calls, backed by memory.
// The person's work as the server shows it: one conversation, each message tagged with the project it belongs to.
(function () {
  const listeners = { event: [], state: [], update: [] };
  const fire = (kind, payload) => listeners[kind].forEach((fn) => fn(payload));
  // A clock that only moves forward, a second per reading: what happened first always sorts first.
  let clock = Date.now() - 3600 * 1000;
  const now = () => new Date(clock = Math.max(clock + 1000, Date.now() - 1800 * 1000)).toISOString();
  const ago = (min) => new Date(Date.now() - min * 60000).toISOString();
  let configured = false;
  let nextId = 1;
  const messages = [];
  const SCHEDULES = [
    { id: 's1', action: 'remind', text: '记得看巡检结果', text_when: '每天 09:00（Asia/Shanghai）' },
    { id: 's2', action: 'run', text: '检查磁盘', text_when: '每天 08:30（Asia/Shanghai）' },
  ];
  const projects = [
    { id: 'ops1', kind: 'ongoing', name: '服务器巡检', goal: '每天检查服务器磁盘', charter: '只读检查，超过 80% 提醒我',
      state: 'active', thread: 'p-ops1', schedules: SCHEDULES, next_at: ago(-600), runs: 3, open_runs: 0, busy: 0,
      question: null, created_at: ago(3000), updated_at: ago(60), last_at: ago(60), timezone: 'Asia/Shanghai' },
    { id: 't-1', kind: 'once', name: '核对 Laser Control Board 的供应商报价', goal: '核对报价', state: 'waiting',
      stage: 'person', action: 'answer', question: 'R12 用 0402 还是 0603 封装？两家报价差 12%。', thread: 'main',
      runs: 1, open_runs: 1, busy: 0, can_retry: false, created_at: ago(30), updated_at: ago(20), last_at: ago(20) },
    { id: 't-2', kind: 'once', name: '整理本周交期变化', goal: '整理交期', state: 'active', stage: 'working', action: null,
      question: null, thread: 'main', runs: 1, open_runs: 1, busy: 1, can_retry: false, created_at: ago(10),
      updated_at: ago(5), last_at: ago(5) },
    { id: 't-3', kind: 'once', name: '更新 BOM v3 的替代料', goal: '替代料', state: 'ended', stage: 'ended',
      outcome: 'completed', question: null, thread: 'main', runs: 1, open_runs: 0, busy: 0, can_retry: false,
      created_at: ago(200), updated_at: ago(190), last_at: ago(190) },
    { id: 't-4', kind: 'once', name: 'R12 报价对比', goal: '列报价', state: 'ended', stage: 'ended', outcome: 'completed',
      question: null, thread: 'main', runs: 1, open_runs: 0, busy: 0, can_retry: false, created_at: ago(100),
      updated_at: ago(99), last_at: ago(99) },
    { id: 't-9', kind: 'once', name: '数一下服务器', goal: '数服务器', state: 'ended', stage: 'ended', outcome: 'completed',
      question: null, thread: 'main', runs: 1, open_runs: 0, busy: 0, can_retry: false, created_at: ago(50),
      updated_at: ago(49), last_at: ago(49) },
  ];
  const ok = (data) => Promise.resolve({ ok: true, data });
  const add = (project, role, text, ticket = null, meta = null, thread = 'main') => {
    const row = { id: nextId++, user: 'me', thread, role, text, ticket_id: ticket, at: now(), meta, project };
    messages.push(row);
    return { ...row };
  };
  const APPROVALS = { ask: '每条命令都先问你', read: '只读命令直接执行，改动先问你', auto: '直接执行' };
  const sshHosts = [
    { name: 'andrew', address: '10.128.0.20', port: 22, user: 'Yuxia', jump: null, approval: 'ask', note: '',
      approval_text: APPROVALS.ask, state: {} },
  ];
  const steps = [];
  const approvals = {};
  const toolList = ['github', 'email', 'slack', 'google', 'cloudflare', 'godaddy']
    .map((kind) => ({ kind, connected: false, account: null, settings: {}, test: null }));
  let nextStep = 1;
  const step = (project, ticket, key, name, input, status, output = '') => {
    let row = steps.find((x) => x.ticket_id === ticket && x.key === key);
    if (!row) {
      row = { id: nextStep++, user: 'me', thread: 'main', ticket_id: ticket, key, name, input, at: now(), project };
      steps.push(row);
    }
    Object.assign(row, { status, output, ended_at: status === 'running' ? null : now() });
    return { ...row };
  };
  add(null, 'user', '帮我核对一下 Laser Control Board 的供应商报价');
  add('t-1', 'acknowledgement', '收到，开始处理：核对 Laser Control Board 的供应商报价', 't-1');
  step('t-1', 't-1', 'k1', 'think', '4', 'done', '先把三家的报价单读进来，按料号对齐，再比单价和封装。');
  step('t-1', 't-1', 'k2', 'read_file', 'quotes/vendor_a.csv', 'done', 'part,package,price\nR12,0402,0.012\n…');
  step('t-1', 't-1', 'k3', 'shell', 'python3 compare.py quotes/', 'done', '[exit code 0]\n4 differences found');
  add('ops1', 'reminder', '记得看巡检结果', null, { project: 'ops1', project_name: '服务器巡检' });
  add('ops1', 'scheduled', '【定时任务】检查磁盘', null, { project: 'ops1', project_name: '服务器巡检' }, 'p-ops1');
  add('ops1', 'reply', '10 台服务器磁盘都在 80% 以下，最高 parts-scraper 67%。', 'r-1', null, 'p-ops1');
  add(null, 'user', '列一下 R12 的两家报价');
  add('t-4', 'reply', '#### R12 报价对比\n| 供应商 | 封装 | 单价 |\n|---|---|---|\n| 甲 | 0402 | **0.012** |\n| 乙 | 0603 | 0.010 |\n- 差价 12%\n- <b>未</b>含运费', 't-4');
  // A project made to be too wide: long commands, a wide table, code, a long URL.
  add(null, 'user', '我现在能看到多少台服务器？');
  step('t-9', 't-9', 'w1', 'shell', 'hostname; whoami; cat /etc/hosts 2>/dev/null; echo "---"; env | grep -i -E "server|host|ssh|inventory" 2>/dev/null | head -20; ls -la ~/.ssh ~/.aws ~/.kube /etc/ssh 2>&1', 'done', '[exit code 0]\n' + 'x'.repeat(400));
  step('t-9', 't-9', 'w2', 'shell', 'echo "HOME=$HOME"\nls -la $HOME 2>&1 | head\necho "=== aws config"; ls -la $HOME/.aws 2>&1', 'done', '[exit code 0]\nok');
  add('t-9', 'reply', [
    '#### 结果', '我这边看不到任何外部服务器，只有 1 台：**ip-172-31-14-189.eu-west-2.compute.internal**，*AWS EC2*。',
    '详见 https://ai-api.yuxianglin.xyz/api/v1/very/long/path/that/keeps/going/and/going/without/any/break/at/all/0123456789 。',
    '[文档](https://example.com/docs)', '> 这是引用，<script>alert(1)</script> 不会执行', '---',
    '| 名字 | 地址 | 说明 | 另一个很长很长的列 | 再来一列 |', '|---|---|---|---|---|',
    '| parts-catalog-deploy-and-something-long | 34.56.245.201 | 超长说明' + '文字'.repeat(30) + ' | aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | b |',
    '```bash', 'for h in $(cat hosts); do ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" hostname || echo "$h unreachable"; done # ' + 'x'.repeat(120), '```',
    '1. 第一', '2. 第二',
  ].join('\n'), 't-9');

  // A new request becomes a one-off project once the assistant takes it up.
  const opened = (ticket, name) => {
    if (!projects.some((p) => p.id === ticket)) {
      projects.unshift({ id: ticket, kind: 'once', name, goal: name, state: 'active', stage: 'working', action: null,
        question: null, thread: 'main', runs: 1, open_runs: 1, busy: 1, can_retry: false, created_at: now(),
        updated_at: now(), last_at: now() });
      fire('event', { type: 'ticket', ticket: { id: ticket, stage: 'working' } });
    }
  };
  const closed = (ticket) => {
    const p = projects.find((x) => x.id === ticket);
    if (p && p.kind === 'once') Object.assign(p, { state: 'ended', stage: 'ended', outcome: 'completed', busy: 0, open_runs: 0 });
    fire('event', { type: 'ticket', ticket: { id: ticket, stage: 'ended' } });
  };

  // A test can hold a turn at its steps (`hold`), then let it go on one step at a time (`release()`). Releases are
  // counted, and a held pause goes on once there are as many as pauses so far: no order of the two loses one.
  let passes = 0;
  let pauses = 0;
  window.__fake = { fire, messages, projects, steps, hold: false,
    release: () => { passes++; },
    gates: () => ({ passes, pauses }) };
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
    everything: (after) => ok(messages.filter((m) => m.id > after).map((m) => ({ ...m }))),
    allSteps: () => ok(steps.map((x) => ({ ...x }))),
    search: (q) => ok(messages.filter((m) => m.text.includes(q)).reverse().map((m) => ({ ...m }))),
    projects: () => ok(projects.map((p) => ({ ...p }))),
    setTimezone: (timezone) => { window.__fake.timezone = timezone; return ok({ timezone }); },
    projectAction: (id, action) => {
      const p = projects.find((x) => x.id === id);
      if (!p) return Promise.resolve({ ok: false, status: 404, error: '没有这个项目' });
      const to = { confirm: 'active', refuse: 'refused', pause: 'paused', resume: 'active', end: 'ended' }[action];
      p.state = to;
      p.next_at = to === 'active' ? ago(-600) : null;
      for (const m of messages) {
        if (m.role === 'proposal' && m.meta && m.meta.project === id) {
          m.meta = { ...m.meta, state: to };
          setTimeout(() => fire('event', { type: 'message-updated', message: { ...m } }), 5);
        }
      }
      if (action === 'confirm') {
        setTimeout(() => fire('event', { type: 'message', message: add(id, 'notice', `项目「${p.name}」开始了，下一次在明天 09:00。`,
          null, { project: id, project_name: p.name }) }), 10);
      }
      return ok({ ...p });
    },
    post: (thread, text) => {
      const project = thread.startsWith('p-') ? thread.slice(2) : null;
      const row = add(project, 'user', text, null, project ? { project, project_name: (projects.find((p) => p.id === project) || {}).name } : null, thread);
      // A turn as the server streams it: acknowledged, thinking, a command run, the answer written as it comes.
      const ticket = project ? `r-${row.id}` : `t-${row.id}`;
      const owner = project || ticket;
      const answer = `已处理「${text}」\n- 交期变化 **2** 处\n- 已写入 \`/work/report.md\``;
      const live = (event) => fire('event', { ...event, ticket_id: ticket, thread, project: owner });
      const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      // Held, the turn stops while the command runs and again while the answer is half written, until released.
      const pause = async () => {
        if (!window.__fake.hold) return;
        const mine = ++pauses;
        while (passes < mine && window.__fake.hold) await wait(20);
      };
      const ack = () => {
        if (!project) opened(ticket, text.slice(0, 20));
        fire('event', { type: 'message', message: add(owner, 'acknowledgement', `收到，开始处理：${text}`, ticket, null, thread) });
      };
      if (text.includes('每天')) {
        // Something to do for a long time: the assistant proposes a project for the person to confirm.
        (async () => {
          await wait(30);
          ack();
          projects.unshift({ id: 'new1', kind: 'ongoing', name: '每日交期提醒', goal: '每天提醒看交期',
            charter: '只提醒，不改任何东西', state: 'proposed', thread: 'p-new1',
            schedules: [{ id: 's9', action: 'remind', text: '看一下今天的交期', text_when: '每天 09:00（Asia/Shanghai）' }],
            next_at: null, runs: 0, open_runs: 0, busy: 0, question: null, created_at: now(), updated_at: now(),
            last_at: now() });
          fire('event', { type: 'message', message: add('new1', 'proposal', '提议一个长期项目：每日交期提醒', ticket,
            { project: 'new1', project_name: '每日交期提醒', state: 'proposed' }) });
          fire('event', { type: 'message', message: add(owner, 'reply', '提议了「每日交期提醒」，请确认。', ticket) });
          closed(ticket);
          live({ type: 'idle' });
        })();
        return ok(row);
      }
      if (text.includes('重启')) {
        // A command on a server that waits for the person's approval, then runs (or does not).
        (async () => {
          await wait(30);
          ack();
          const input = 'web  $ systemctl restart nginx';
          fire('event', { type: 'step', step: step(owner, ticket, 'r', 'ssh', input, 'approval', 'nginx 没在跑，重启一下') });
          const approve = await new Promise((resolve) => { approvals[`${ticket}:r`] = resolve; });
          fire('event', { type: 'step', step: step(owner, ticket, 'r', 'ssh', input, approve ? 'done' : 'error',
            approve ? '[exit code 0]\n' : '人拒绝了这条命令') });
          fire('event', { type: 'message', message: add(owner, 'reply', approve ? '已重启 nginx。' : '你拒绝了，没有重启。', ticket, null, thread) });
          live({ type: 'idle' });
        })();
        return ok(row);
      }
      (async () => {
        await wait(30);
        ack();
        live({ type: 'thinking' });
        await wait(90);
        fire('event', { type: 'step', step: step(owner, ticket, 'a', 'shell', 'ls /work', 'running') });
        await wait(140);
        await pause();
        fire('event', { type: 'step',
          step: step(owner, ticket, 'a', 'shell', 'ls /work', 'done', '[exit code 0]\nbom.csv\nleadtimes.csv') });
        live({ type: 'thinking' });
        await wait(60);
        for (let n = 4; n < answer.length + 4; n += 4) {
          live({ type: 'stream', text: answer.slice(0, n) });
          await wait(25);
        }
        await pause();
        fire('event', { type: 'message', message: add(owner, 'reply', answer, ticket, null, thread) });
        live({ type: 'idle' });
      })();
      return ok(row);
    },
    approve: (ticketId, key, approve) => {
      const resolve = approvals[`${ticketId}:${key}`];
      if (!resolve) return Promise.resolve({ ok: false, status: 404, error: '这条命令已经不在等批准了' });
      delete approvals[`${ticketId}:${key}`];
      setTimeout(() => resolve(approve), 20);
      return ok({ decided: approve ? 'approved' : 'refused' });
    },
    workers: () => {
      const at = (min) => new Date(Date.now() - min * 60000).toISOString();
      return ok({
        executor: 'agent', max_workers: 30, busy_now: 3, crew_waiting: 0,
        crew: [
          { id: 'c1', ticket_id: 't-2', title: '整理本周交期变化', label: '1·供应商甲', task: '整理供应商甲的交期',
            started_at: at(2), step: { name: 'shell', input: 'python3 leadtimes.py a', status: 'running' } },
          { id: 'c2', ticket_id: 't-2', title: '整理本周交期变化', label: '2·供应商乙', task: '整理供应商乙的交期',
            started_at: at(2), step: null },
        ],
        totals: { assignments: 7, tickets: 5, running: 2, waiting: 1, pending: 0 },
        tools: { shell: 12, read_file: 3, write_file: 2, claude_code: 2, codex: 1, ssh: 4 },
        running: [
          { ticket_id: 't-2', title: '整理本周交期变化', thread: 'main', assignment_id: 'a2', number: 1, state: 'running',
            busy: true, crew: 2, started_at: at(3), created_at: at(3), steps: { shell: 2, claude_code: 1 },
            current: { name: 'claude_code', input: '把交期表整理成 markdown 表格', status: 'running' } },
          { ticket_id: 't-1', title: '核对 Laser Control Board 的供应商报价', thread: 'main', assignment_id: 'a1',
            number: 2, state: 'running', busy: false, started_at: at(40), created_at: at(40), steps: { shell: 1 },
            current: null },
        ],
        recent: [
          { ticket_id: 't-3', title: '更新 BOM v3 的替代料', thread: 'main', assignment_id: 'a3', number: 1, state: 'ended',
            outcome: 'completed', started_at: at(90), ended_at: at(84), created_at: at(90),
            steps: { shell: 5, write_file: 2, ssh: 4 } },
        ],
      });
    },
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
    sshProfile: (name) => new Promise((resolve) => setTimeout(() => {
      const h = sshHosts.find((x) => x.name === name);
      h.profile = { ok: true, updated_at: now(), text: `${name} 是 ERP 的生产机。\n- 服务：nginx（443）、erp-api\n- 项目：/home/u/erp，分支 feature/mrp，在做 MRP` };
      resolve({ ok: true, data: { ...h } });
    }, 100)),
    tools: () => ok({ admin: true, tools: toolList.map((t) => ({ ...t })) }),
    toolSave: (kind, fields) => {
      const t = toolList.find((x) => x.kind === kind);
      if (kind === 'github' && !fields.token && !t.connected) return Promise.resolve({ ok: false, status: 422, error: '还没有填凭据' });
      Object.assign(t, { connected: true, account: kind === 'github' ? 'YuxiangLin-kaslana' : 'ok',
        test: { ok: true, message: '已连接', at: now() } });
      window.__fake.savedTools = { ...(window.__fake.savedTools || {}), [kind]: fields };
      return ok({ ...t });
    },
    toolTest: (kind) => ok({ ...toolList.find((x) => x.kind === kind) }),
    toolRemove: (kind) => {
      Object.assign(toolList.find((x) => x.kind === kind), { connected: false, account: null, test: null });
      return ok({ removed: true });
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
      const p = projects.find((x) => x.id === id);
      Object.assign(p, { state: 'ended', stage: 'ended', outcome: 'stopped', question: null, busy: 0 });
      return ok({ id, stage: 'ended', outcome: 'stopped' });
    },
    retry: (id) => ok({ id }),
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
