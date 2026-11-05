# Widget sandbox contract

Widgets are small, self-contained HTML tools that Lantern writes for you (`make_widget`) or that ship with it (`widgets/`). They are untrusted code, so three independent layers keep them harmless.

## 1. Static checker (`lantern/widgets.py`)

Runs when a widget is generated, saved, and served (files on disk can change). It rejects:

- **Network:** `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `sendBeacon`, workers and `importScripts`.
- **Code loading:** `eval`, `Function`, string timers, dynamic and static `import`, module scripts.
- **External resources:** `<script src>`, `<link>`, `<iframe>`, `<frame>`, `<object>`, `<embed>`, `<base>`, `srcdoc`, forms with an `action`, meta refresh, and any `http(s)://`, `ws(s)://`, `ftp://` or `//host` URL.
- **Escape hatches:** `document.cookie`, `top`, `parent`, `opener`, `frameElement`, raw `postMessage`, `localStorage`, `sessionStorage`, `indexedDB`, `caches`, `location` changes, `open('…')`.
- **Obfuscation:** `window[...]`, `globalThis[...]`, `.constructor`, `\x..` and `\u....` escapes, `atob`, `fromCharCode`.
- **Content:** anything that reads as health or medical advice.
- **Size:** anything over 100 KB.

A generated widget that fails gets one retry (the model sees the findings), then it is refused.

## 2. Sandbox (desktop)

```
panel.html (CSP: script-src 'self'; connect-src 127.0.0.1 only)
 └─ <iframe sandbox="allow-scripts" src="widget-host.html">     opaque origin, no same-origin
     CSP: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:
     └─ <iframe sandbox="allow-scripts" srcdoc="bridge + widget">   inherits the host's CSP
```

- There is never `allow-same-origin`, `allow-top-navigation`, `allow-popups` or `allow-forms`.
- No network: `default-src 'none'` blocks fetch, XHR and WebSocket even if the checker missed something.
- No access to the panel, Lantern's token or the local server: the frames have opaque origins.

## 3. Bridge (the only API)

```js
lantern.notify(text)                 // a short message in the panel (max 200 chars)
await lantern.storage.get(key)       // this widget's own saved value, or null
lantern.storage.set(key, value)      // save a small JSON value (4 KB each, 50 keys per widget)
```

The host only relays these messages. The panel re-checks each one (`desktop/renderer/lib/widgets.js`) and namespaces storage per widget.

## Saving

Saving needs approval, bound to the widget's name and the SHA-256 of its HTML. It is written to `~/.lantern/widgets/<name>.html` and verified on disk. Your widgets override built-ins with the same name.

## Pre-made

`widgets/water-timer.html`: a timer (minutes, start/pause/reset, a progress ring) that tells a joke when it rings. It is a plain timer and gives no health advice.
