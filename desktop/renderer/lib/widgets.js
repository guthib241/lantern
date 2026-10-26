/* Widget sandbox contract (see docs/WIDGETS.md). Pure helpers shared by the panel and tests.
 * A widget runs in <iframe sandbox="allow-scripts"> (never allow-same-origin) inside
 * widget-host.html, whose CSP is default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'.
 * The only bridge is postMessage with three messages: notify, storage.get, storage.set. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternWidgets = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const SANDBOX = 'allow-scripts';
  const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:";
  const MAX_TEXT = 200;
  const MAX_KEY = 64;
  const MAX_VALUE = 4000;
  const MAX_KEYS = 50;

  /** Returns {ok: true, msg} with a cleaned message, or {ok: false, error}. */
  function check(msg) {
    if (!msg || typeof msg !== 'object') return { ok: false, error: 'not an object' };
    const type = msg.type;
    if (type === 'notify') {
      if (typeof msg.text !== 'string' || !msg.text.trim()) return { ok: false, error: 'notify needs text' };
      return { ok: true, msg: { type, text: msg.text.slice(0, MAX_TEXT) } };
    }
    if (type === 'storage.get' || type === 'storage.set') {
      if (typeof msg.key !== 'string' || !/^[\w.-]{1,64}$/.test(msg.key)) return { ok: false, error: 'bad key' };
      if (type === 'storage.get') return { ok: true, msg: { type, key: msg.key, id: msg.id } };
      const value = JSON.stringify(msg.value === undefined ? null : msg.value);
      if (value.length > MAX_VALUE) return { ok: false, error: 'value too big' };
      return { ok: true, msg: { type, key: msg.key, value: JSON.parse(value), id: msg.id } };
    }
    return { ok: false, error: `message type ${String(type).slice(0, 30)} is not allowed` };
  }

  /** Per-widget storage on top of any {getItem, setItem} store, namespaced and capped. */
  function storage(store, widgetName) {
    const prefix = `lantern.widget.${widgetName}.`;
    const keysKey = prefix + '__keys';
    const keys = () => JSON.parse(store.getItem(keysKey) || '[]');
    return {
      get(key) {
        const raw = store.getItem(prefix + key);
        return raw == null ? null : JSON.parse(raw);
      },
      set(key, value) {
        const k = keys();
        if (!k.includes(key)) {
          if (k.length >= MAX_KEYS) throw new Error('widget storage is full');
          k.push(key);
          store.setItem(keysKey, JSON.stringify(k));
        }
        store.setItem(prefix + key, JSON.stringify(value));
      },
    };
  }

  return { SANDBOX, CSP, check, storage, MAX_KEY, MAX_VALUE };
});
