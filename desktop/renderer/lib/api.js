/* Talks to the local Lantern server (127.0.0.1) with the per-install token.
 * events(onEvent) keeps a websocket open and reconnects with backoff. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternApi = api;
})(typeof self !== 'undefined' ? self : this, function () {
  class Api {
    constructor(base, token, opts) {
      this.base = String(base || '').replace(/\/$/, '');
      this.token = token || '';
      this.fetch = (opts && opts.fetch) || ((...a) => fetch(...a));
      this.WS = (opts && opts.WebSocket) || (typeof WebSocket !== 'undefined' ? WebSocket : null);
      this.socket = null;
      this.closed = false;
    }

    async request(method, path, body) {
      const headers = { 'X-Lantern-Token': this.token };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const r = await this.fetch(this.base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await r.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
      if (!r.ok) {
        const err = new Error((data && data.detail) || `HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return data;
    }

    get(path) { return this.request('GET', path); }
    post(path, body) { return this.request('POST', path, body === undefined ? {} : body); }

    async blob(path) {
      const headers = { 'X-Lantern-Token': this.token };
      const r = await this.fetch(this.base + path, { headers });
      if (!r.ok) {
        const err = new Error(`HTTP ${r.status}`);
        err.status = r.status;
        throw err;
      }
      return r.blob();
    }

    async raw(path) {
      const headers = { 'X-Lantern-Token': this.token };
      const r = await this.fetch(this.base + path, { method: 'GET', headers });
      const text = await r.text();
      if (!r.ok) {
        let detail = text;
        try { detail = JSON.parse(text).detail || text; } catch { /* keep the raw text */ }
        const err = new Error(detail);
        err.status = r.status;
        throw err;
      }
      return text;
    }

    events(onEvent, onState) {
      let delay = 500;
      const open = () => {
        if (this.closed || !this.WS) return;
        const url = this.base.replace(/^http/, 'ws') + '/events?token=' + encodeURIComponent(this.token);
        const ws = new this.WS(url);
        this.socket = ws;
        ws.onopen = () => { delay = 500; if (onState) onState(true); };
        ws.onmessage = (m) => {
          try { onEvent(JSON.parse(m.data)); } catch (e) { /* ignore malformed events */ }
        };
        ws.onclose = () => {
          if (onState) onState(false);
          if (!this.closed) setTimeout(open, delay);
          delay = Math.min(delay * 2, 8000);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) { /* already closed */ } };
      };
      open();
      return () => { this.closed = true; if (this.socket) this.socket.close(); };
    }
  }
  return { Api };
});
