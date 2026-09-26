/* Local service boundary. The UI consumes this interface, not a provider SDK.
   Tests may inject the same interface before this script is loaded. */
(() => {
  if (window.werkbankServices) return;
  let token = '', revision = -1, instance = '', documents = {}, flight = null;
  let online = false, started = null, preferenceTimer = null, preferenceFlight = null;
  const listeners = new Set(), pending = new Map(), preferences = new Map();
  const clone = x => JSON.parse(JSON.stringify(x));
  const signal = () => window.dispatchEvent(new CustomEvent('werkbank-storage', {detail: {online, pending: pending.size}}));

  async function session() {
    const response = await fetch('/api/session', {cache: 'no-store'});
    if (!response.ok) throw Object.assign(new Error('Lokaler Dienst ist nicht erreichbar.'), {code: 'connection'});
    token = (await response.json()).token;
  }
  async function request(path, options = {}, retry = true) {
    const writing = options.method && options.method !== 'GET';
    try {
      const response = await fetch(path, {...options, cache: 'no-store',
        headers: {...(writing ? {'X-Werkbank-Token': token} : {}), ...options.headers}});
      if (response.status === 401 && retry) { await session(); return request(path, options, false); }
      const result = await response.json();
      if (!response.ok) throw Object.assign(new Error(result.message || 'Lokaler Vorgang fehlgeschlagen.'), {code: result.code || 'http_error'});
      online = true; signal(); return result;
    } catch (error) {
      if (!error.code) { online = false; error.code = 'connection'; }
      signal(); throw error;
    }
  }
  const json = (method, value) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(value)});
  const snapshot = (path, value) => ({id: path.split('/')[1], exists: value !== undefined,
    data: () => value === undefined ? undefined : clone(value)});

  function notify() {
    for (const listener of listeners) {
      const pairs = Object.entries(documents).filter(([path]) => path.startsWith(listener.name + '/'));
      if (listener.order) pairs.sort((a, b) => String(a[1][listener.order] || '').localeCompare(String(b[1][listener.order] || '')) || a[0].localeCompare(b[0]));
      const signature = JSON.stringify(pairs);
      if (signature !== listener.signature) {
        listener.signature = signature;
        listener.callback({docs: pairs.map(([path, value]) => snapshot(path, value)), size: pairs.length, empty: !pairs.length});
      }
    }
  }
  async function refresh() {
    if (flight) return flight;
    flight = (async () => {
      const state = await request('/api/state?since=' + revision);
      instance = state.instance; revision = state.revision;
      if (state.documents) {
        documents = state.documents;
        for (const [path, value] of Object.entries(documents)) {
          if (path.startsWith('ui/') && !pending.has(path.slice(3))) preferences.set(path.slice(3), value.value);
        }
        notify();
      }
      return state;
    })();
    try { return await flight; } finally { flight = null; }
  }
  const collection = (name, order) => ({
    orderBy: field => collection(name, field),
    onSnapshot(callback, error) {
      const listener = {name, order, callback, error, signature: null};
      listeners.add(listener); queueMicrotask(notify);
      return () => listeners.delete(listener);
    }
  });
  const db = {
    collection,
    doc: path => ({
      get: async () => { await refresh(); return snapshot(path, documents[path]); },
      set: async value => { await request('/api/doc/' + path, json('PUT', value)); await refresh(); }
    })
  };
  function rememberPending() {
    try {
      const key = 'wb:pending:' + instance;
      if (pending.size) localStorage.setItem(key, JSON.stringify(Object.fromEntries(pending)));
      else localStorage.removeItem(key);
    } catch (_) { /* The server is the persistent store; status still reports pending writes. */ }
  }
  async function flush() {
    clearTimeout(preferenceTimer);
    if (preferenceFlight) return preferenceFlight;
    preferenceFlight = (async () => {
      while (pending.size) {
        const [key, value] = pending.entries().next().value;
        await request('/api/doc/ui/' + key, json('PUT', {value}));
        if (JSON.stringify(pending.get(key)) === JSON.stringify(value)) pending.delete(key);
        rememberPending(); signal();
      }
    })();
    try { await preferenceFlight; } finally { preferenceFlight = null; }
  }
  const prefs = {
    get(key, fallback) { return preferences.has(key) ? clone(preferences.get(key)) : fallback; },
    set(key, value) {
      if (JSON.stringify(preferences.get(key)) === JSON.stringify(value)) return;
      preferences.set(key, clone(value)); pending.set(key, clone(value)); rememberPending(); signal();
      clearTimeout(preferenceTimer); preferenceTimer = setTimeout(() => flush().catch(() => {}), 180);
    },
    flush
  };
  window.addEventListener('pagehide', () => {
    for (const [key, value] of pending) {
      fetch('/api/doc/ui/' + key, {...json('PUT', {value}), keepalive: true,
        headers: {'Content-Type': 'application/json', 'X-Werkbank-Token': token}}).catch(() => {});
    }
  });
  window.werkbankServices = {
    connect() {
      if (started) return started;
      started = (async () => {
        await session(); await refresh();
        try {
          const saved = JSON.parse(localStorage.getItem('wb:pending:' + instance) || '{}');
          for (const [key, value] of Object.entries(saved)) { preferences.set(key, value); pending.set(key, value); }
        } catch (_) { /* invalid pending browser cache does not replace server data */ }
        if (pending.size) flush().catch(() => {});
        setInterval(() => refresh().then(() => pending.size && flush()).catch(() => {}), 900);
        return {
          db, preferences: prefs,
          status: async () => ({online, pending: pending.size, agentConnected: false}),
          messages: {submit: async (id, data) => {
            const result = await request('/api/messages', json('POST', {id, data})); await refresh(); return result;
          }},
          assets: {upload: blob => request('/api/assets', {method: 'POST', body: blob})},
          sync: async () => { const r = await request('/api/sync', json('POST', {})); await refresh(); return r; },
          export: async () => {
            await flush();
            const response = await fetch('/api/export', {cache: 'no-store'});
            if (!response.ok) throw new Error('Sicherung konnte nicht erstellt werden.');
            return response.blob();
          },
          import: async file => request('/api/import', {method: 'POST', body: file})
        };
      })();
      return started;
    }
  };
})();
