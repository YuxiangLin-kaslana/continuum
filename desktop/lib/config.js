// Where the app keeps its connection: the server address in plain text, the token encrypted by the OS
// (safeStorage: DPAPI on Windows) when it is available.
const fs = require('fs');
const path = require('path');

function create({ app, safeStorage }) {
  const file = path.join(app.getPath('userData'), 'connection.json');

  function read() {
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      return {};
    }
  }

  function load() {
    const saved = read();
    let token = null;
    if (saved.token && saved.encrypted && safeStorage.isEncryptionAvailable()) {
      try {
        token = safeStorage.decryptString(Buffer.from(saved.token, 'base64'));
      } catch {
        token = null;
      }
    } else if (saved.token && !saved.encrypted) {
      token = saved.token;
    }
    return { server: saved.server || '', user: saved.user || '', token };
  }

  function save({ server, user, token }) {
    const encrypted = safeStorage.isEncryptionAvailable();
    const stored = encrypted ? safeStorage.encryptString(token).toString('base64') : token;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify({ server, user, token: stored, encrypted }, null, 2));
  }

  function clear() {
    const { server } = read();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify({ server: server || '' }, null, 2));
  }

  return { load, save, clear };
}

module.exports = { create };
