// The main process's API client against a real Continuum server (tests/test_desktop.py starts one and sets
// CONTINUUM_URL and CONTINUUM_TOKEN).
const { test } = require('node:test');
const assert = require('node:assert');
const { Client } = require('../lib/client');

const server = process.env.CONTINUUM_URL;
const token = process.env.CONTINUUM_TOKEN;

async function waitFor(predicate, ms = 5000) {
  const end = Date.now() + ms;
  while (!predicate()) {
    if (Date.now() > end) throw new Error('timed out');
    await new Promise((r) => setTimeout(r, 20));
  }
}

test('checks a token before it is saved', async () => {
  const me = await Client.verify(fetch, `${server}/`, token);
  assert.equal(me.user, 'me');
  await assert.rejects(Client.verify(fetch, server, 'ctm_wrong'), (error) => error.status === 401);
  await assert.rejects(Client.verify(fetch, 'http://127.0.0.1:9', token), (error) => error.status === 0);
});

test('logs in with a password, gets a token of its own, and gives it back on logout', async () => {
  await assert.rejects(Client.login(fetch, server, 'me', 'wrong password', 'test'), (error) => error.status === 401);
  const session = await Client.login(fetch, server, 'me', process.env.CONTINUUM_PASSWORD, 'test device');
  assert.equal(session.user, 'me');
  assert.notEqual(session.token, token);
  assert.equal((await Client.verify(fetch, server, session.token)).user, 'me');
  const client = new Client({ fetch, onEvent() {}, onState() {} });
  client.configure(server, session.token);
  await client.logout();
  await assert.rejects(Client.verify(fetch, server, session.token), (error) => error.status === 401);
});

test('sends a message and hears the acknowledgement, the reply and the ticket on the stream', async () => {
  const events = [];
  const states = [];
  const client = new Client({ fetch, onEvent: (e) => events.push(e), onState: (s) => states.push(s) });
  client.configure(server, token);
  client.connect();
  await waitFor(() => states.includes('online'));
  const row = await client.request('POST', '/api/v1/threads/main/messages', { text: '检查报价' });
  assert.equal(row.role, 'user');
  await waitFor(() => events.some((e) => e.type === 'message' && e.message.role === 'reply'));
  assert.deepEqual(events.filter((e) => e.type === 'message').map((e) => e.message.role),
    ['user', 'acknowledgement', 'reply']);
  assert.ok(events.some((e) => e.type === 'ticket' && e.ticket.outcome === 'completed'));
  const tickets = await client.request('GET', '/api/v1/tickets');
  assert.equal(tickets.length, 1);
  client.disconnect();
});

test('reports an unauthorized stream and stops retrying', async () => {
  const states = [];
  const client = new Client({ fetch, onEvent() {}, onState: (s) => states.push(s) });
  client.configure(server, 'ctm_wrong');
  client.connect();
  await waitFor(() => states.includes('unauthorized'));
  assert.equal(client.closed, true);
});
