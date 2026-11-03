// The API client, in the main process: HTTP calls with the bearer token, and one WebSocket stream that reconnects
// with a growing delay. The renderer never sees the token.
const WebSocket = require('ws');

function normalize(server) {
  return String(server || '').trim().replace(/\/+$/, '');
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

class Client {
  constructor({ fetch, onEvent, onState }) {
    this.fetch = fetch;
    this.onEvent = onEvent;
    this.onState = onState;
    this.server = '';
    this.token = null;
    this.socket = null;
    this.retry = 1000;
    this.timer = null;
    this.closed = true;
  }

  configure(server, token) {
    this.disconnect();
    this.server = normalize(server);
    this.token = token;
  }

  async request(method, route, body) {
    if (!this.server || !this.token) throw new ApiError(0, '还没有连接到服务器');
    let response;
    try {
      response = await this.fetch(`${this.server}${route}`, {
        method,
        headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      throw new ApiError(0, `连不上服务器：${error.message}`);
    }
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const detail = data && data.detail;
      const message = typeof detail === 'string' ? detail : (detail && detail.message) || `请求失败（${response.status}）`;
      throw new ApiError(response.status, message);
    }
    return data;
  }

  // Check an address and token before saving them.
  static async verify(fetch, server, token) {
    const client = new Client({ fetch, onEvent() {}, onState() {} });
    client.configure(server, token);
    return client.request('GET', '/api/v1/me');
  }

  connect() {
    this.closed = false;
    this.open();
  }

  open() {
    if (this.closed || !this.server || !this.token) return;
    const url = `${this.server.replace(/^http/, 'ws')}/api/v1/stream?token=${encodeURIComponent(this.token)}`;
    this.onState('connecting');
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.on('open', () => {
      this.retry = 1000;
      this.onState('online');
    });
    socket.on('message', (raw) => {
      let event;
      try {
        event = JSON.parse(String(raw));
      } catch {
        return;
      }
      if (event.type !== 'ping') this.onEvent(event);
    });
    const lost = (code) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (code === 4401) {
        this.closed = true;
        this.onState('unauthorized');
        return;
      }
      this.onState('offline');
      this.schedule();
    };
    socket.on('close', (code) => lost(code));
    // A server that refuses the upgrade outright answers with HTTP 401 or 403.
    socket.on('unexpected-response', (_request, response) => {
      lost(response.statusCode === 401 || response.statusCode === 403 ? 4401 : 1006);
      socket.terminate();
    });
    socket.on('error', () => {});
  }

  schedule() {
    if (this.closed || this.timer) return;
    const delay = this.retry;
    this.retry = Math.min(this.retry * 2, 30000);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.open();
    }, delay);
  }

  disconnect() {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.socket) {
      const socket = this.socket;
      this.socket = null;
      socket.close();
    }
  }
}

module.exports = { Client, ApiError, normalize };
