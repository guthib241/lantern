"""Lantern's own browser: Playwright Chromium with a dedicated profile.

  - A separate profile in ~/.lantern/browser-profile with the password manager and
    autofill turned off, so there are no saved passwords or cards to leak.
  - 1280x800 viewport at device scale 1, so screenshot pixels == click coordinates.
  - Form submission is blocked inside the page (init script) until Lantern sets
    the allow flag right before the approved Submit click. Pressing Enter in a
    field or a stray click on "Submit" by the model only bumps a counter.
  - Field discovery reads labels the way a person would (label, aria-label,
    aria-labelledby, placeholder) and tags each field with data-lantern-id.

The helper JavaScript is sent fresh with every call instead of living on `window`,
so the page can't swap it out between calls.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

WIDTH, HEIGHT = 1280, 800
JPEG_QUALITY = 70
DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium"  # Claude Code cloud sandbox; laptops use Playwright's own

# Chromium preferences for a profile with no password manager and no autofill.
PROFILE_PREFS = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"enabled": False, "profile_enabled": False, "credit_card_enabled": False},
}
LAUNCH_ARGS = ["--password-store=basic", "--disable-save-password-bubble", "--no-first-run",
               "--no-default-browser-check", "--disable-features=AutofillServerCommunication"]

SUBMIT_GUARD = """
(() => {
  if (window.__lanternGuard) return;
  window.__lanternGuard = true;
  window.__lanternBlocked = 0;
  window.__lanternSubmitAllowed = false;
  window.addEventListener('submit', (e) => {
    if (!window.__lanternSubmitAllowed) { e.preventDefault(); e.stopImmediatePropagation(); window.__lanternBlocked += 1; }
  }, true);
  const nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    if (window.__lanternSubmitAllowed) return nativeSubmit.call(this);
    window.__lanternBlocked += 1;
  };
})();
"""

HELPERS = r"""
const H = {
  vis(el) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  },
  txt(el) { return el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : ''; },
  isControl(el) { return el.matches('input, select, textarea, [contenteditable=""], [contenteditable=true], [role=combobox], [role=textbox]'); },
  label(el) {
    if (el.labels && el.labels.length) return H.txt(el.labels[0]);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => H.txt(document.getElementById(i))).join(' ').trim(); if (t) return t; }
    const al = el.getAttribute('aria-label'); if (al) return al.trim();
    const wrap = el.closest('label'); if (wrap) return H.txt(wrap);
    if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
    if (el.title) return el.title.trim();
    return el.getAttribute('name') || el.id || '';
  },
  groupLabel(el) {
    const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend');
    if (lg) return H.txt(lg);
    const rg = el.closest('[role=radiogroup]');
    if (rg) return rg.getAttribute('aria-label') || H.txt(document.getElementById(rg.getAttribute('aria-labelledby') || ''));
    return el.getAttribute('name') || '';
  },
  tag(el) {
    let v = el.getAttribute('data-lantern-id');
    if (!v) { window.__lanternNext = (window.__lanternNext || 0) + 1; v = 'f' + window.__lanternNext; el.setAttribute('data-lantern-id', v); }
    return v;
  },
  captcha(el) { return !!el.closest('[id*=captcha i],[class*=captcha i],[name*=captcha i],[data-sitekey]'); },
  typeOf(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') return (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'select' || tag === 'textarea') return tag;
    if (el.getAttribute('role') === 'combobox') return 'custom';
    return 'richtext';
  },
  value(el, type) {
    if (type === 'checkbox') return el.checked ? 'checked' : '';
    if (type === 'select') { const o = el.options[el.selectedIndex]; return o && o.value !== '' ? (H.txt(o) || o.value) : ''; }
    if (type === 'file') return [...(el.files || [])].map(f => f.name).join(', ');
    if (type === 'custom' || type === 'richtext') return el.getAttribute('aria-valuetext') || H.txt(el);
    return el.value || '';
  },
  submitLike(el) {
    const b = el.closest('button, input[type=submit], input[type=image], [role=button], a');
    if (!b) return false;
    if (b.matches('input[type=submit], input[type=image]')) return true;
    if (b.tagName === 'BUTTON' && b.type === 'submit' && b.form) return true;
    const t = (b.innerText || b.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    return /^(submit|send|pay|place order|purchase|buy now|complete (purchase|order|payment)|confirm (and|&) pay)\b/.test(t);
  },
  describe(el) {
    const control = el.closest('input, select, textarea, [contenteditable=""], [contenteditable=true], [role=combobox], [role=textbox]');
    const d = {tag: el.tagName.toLowerCase(), captcha: H.captcha(el), submit: H.submitLike(el),
               text: H.txt(el).slice(0, 80)};
    if (control) {
      Object.assign(d, {control: true, type: H.typeOf(control), label: H.label(control).slice(0, 120),
        name: control.getAttribute('name') || '', html_id: control.id || '',
        placeholder: control.getAttribute('placeholder') || '', aria_label: control.getAttribute('aria-label') || '',
        autocomplete: control.getAttribute('autocomplete') || ''});
    }
    if (d.tag === 'iframe') d.frame = [el.src, el.title, el.name, el.id].join(' ');
    return d;
  },
  discover() {
    const fields = [], groups = {};
    const nodes = document.querySelectorAll('input, select, textarea, [role=combobox], [contenteditable=""], [contenteditable=true], [role=textbox]');
    for (const el of nodes) {
      const tag = el.tagName.toLowerCase();
      const type = H.typeOf(el);
      if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
      if (!['input', 'select', 'textarea'].includes(tag) && el.querySelector('input, select, textarea')) continue;
      if (el.disabled || el.readOnly) continue;
      if (type !== 'file' && !H.vis(el)) continue;
      const base = {name: el.getAttribute('name') || '', html_id: el.id || '', type, tag,
                    autocomplete: el.getAttribute('autocomplete') || '',
                    placeholder: el.getAttribute('placeholder') || '', aria_label: el.getAttribute('aria-label') || '',
                    required: !!(el.required || el.getAttribute('aria-required') === 'true'), captcha: H.captcha(el)};
      if (type === 'radio') {
        const key = base.name || H.tag(el);
        let g = groups[key];
        if (!g) {
          g = groups[key] = Object.assign(base, {id: H.tag(el), label: H.groupLabel(el).slice(0, 120), options: [], value: ''});
          g.selector = base.name ? `input[type=radio][name="${CSS.escape(base.name)}"]` : `[data-lantern-id="${g.id}"]`;
          fields.push(g);
        }
        const oid = H.tag(el);
        g.options.push({value: el.value, text: H.label(el), selector: `[data-lantern-id="${oid}"]`});
        if (el.checked) g.value = H.label(el);
        g.required = g.required || base.required;
        continue;
      }
      const id = H.tag(el);
      const f = Object.assign(base, {id, selector: `[data-lantern-id="${id}"]`, label: H.label(el).slice(0, 120),
                                     value: H.value(el, type).slice(0, 500)});
      if (type === 'select') f.options = [...el.options].filter(o => o.value !== '').map(o => ({value: o.value, text: H.txt(o)}));
      if (type === 'custom') {
        const lb = document.getElementById(el.getAttribute('aria-controls') || '') ||
                   (el.parentElement && el.parentElement.querySelector('[role=listbox]'));
        f.options = lb ? [...lb.querySelectorAll('[role=option]')].map(o => ({value: o.getAttribute('data-value') || H.txt(o), text: H.txt(o)})) : [];
      }
      if (type === 'file') f.accept = el.getAttribute('accept') || '';
      fields.push(f);
    }
    const frames = [...document.querySelectorAll('iframe')].filter(H.vis).map(fr => {
      const id = H.tag(fr);
      return {id, selector: `[data-lantern-id="${id}"]`, type: 'frame', label: fr.title || fr.name || 'embedded frame',
              frame: [fr.src, fr.title, fr.name, fr.id].join(' ')};
    });
    return {fields, frames, title: document.title, url: location.href};
  },
  captchaPresent() {
    if (document.querySelector('[id*=captcha i],[class*=captcha i],[name*=captcha i],[data-sitekey],' +
        'iframe[src*=captcha i],iframe[title*=captcha i],iframe[src*=turnstile i],iframe[src*="challenges.cloudflare"]')) return true;
    return /i['’]?m not a robot/i.test(document.body ? document.body.innerText : '');
  },
  findSubmit() {
    let best = null, bestCount = -1;
    for (const f of document.forms) { const c = f.querySelectorAll('input, select, textarea').length; if (c > bestCount) { best = f; bestCount = c; } }
    const scope = best || document;
    let cands = [...scope.querySelectorAll('button[type=submit], input[type=submit], button:not([type])')].filter(H.vis);
    if (!cands.length && best && best.id) cands = [...document.querySelectorAll(`[form="${CSS.escape(best.id)}"]`)].filter(H.vis);
    if (!cands.length) return null;
    // Multi-page forms put Back/Previous/Cancel next to Next as submit buttons too: never pick those.
    const isBack = e => e.hasAttribute('formnovalidate') || /\b(back|previous|prev|cancel)\b|←/i.test(`${e.value || ''} ${H.txt(e)}`);
    const b = cands.find(e => !isBack(e)) || cands[0];
    document.querySelectorAll('[data-lantern-submit]').forEach(e => e.removeAttribute('data-lantern-submit'));
    b.setAttribute('data-lantern-submit', '1');
    return {selector: '[data-lantern-submit="1"]', label: (H.txt(b) || b.value || 'Submit').slice(0, 60)};
  },
  hidden() {
    return [...document.querySelectorAll('input[type=hidden]')].map(e => (e.name || e.id || '') + '=' + e.value).sort().join('\n');
  },
};
"""

KEY_NAMES = {
    "enter": "Enter", "return": "Enter", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "backspace": "Backspace", "delete": "Delete", "del": "Delete", "space": "Space", " ": "Space",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown", "page_up": "PageUp",
    "page_down": "PageDown", "insert": "Insert",
    "ctrl": "Control", "control": "Control", "alt": "Alt", "option": "Alt", "shift": "Shift",
    "cmd": "Meta", "command": "Meta", "meta": "Meta", "super": "Meta", "win": "Meta",
}
MODIFIERS = {"Control", "Alt", "Shift", "Meta"}


class DriverError(RuntimeError):
    """The browser couldn't do what was asked."""


def playwright_key(keys: list[str] | str) -> str:
    """['CTRL', 'A'] / 'ctrl+a' / 'Return' -> 'Control+a' / 'Control+a' / 'Enter'."""
    parts = keys.split("+") if isinstance(keys, str) else list(keys)
    out = []
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        name = KEY_NAMES.get(p.lower())
        if name is None:
            name = p.lower() if len(p) == 1 else (p[:1].upper() + p[1:])
            if re.fullmatch(r"f\d{1,2}", p.lower()):
                name = p.upper()
        out.append(name)
    if not out:
        raise DriverError("empty key press")
    if len(out) > 1 and "Shift" in out and len(out[-1]) == 1:
        out[-1] = out[-1].upper()
    return "+".join(out)


def iso_date(value: str) -> str:
    """Best effort: turn '25/10/2026', 'Oct 25 2026', '25 October 2026' into '2026-10-25'."""
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%b %d %Y", "%b %d, %Y", "%B %d %Y",
                "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v


def chromium_executable(setting: str = "") -> str | None:
    """Explicit setting > LANTERN_CHROMIUM > the cloud sandbox's copy > Playwright's own download."""
    for candidate in (setting, os.environ.get("LANTERN_CHROMIUM", ""), DEFAULT_CHROMIUM):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def prepare_profile(profile_dir: Path) -> None:
    """Create the dedicated profile with the password manager and autofill switched off."""
    prefs = profile_dir / "Default" / "Preferences"
    if not prefs.exists():
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text(json.dumps(PROFILE_PREFS), encoding="utf-8")


# S8: what the user does in Lantern's browser while they teach it a task. Every page reports its fields when it
# loads and again at each click on a button or link (values included, except password fields, which never leave
# the page), plus each field the user changes. recorder.py turns this into the traces S4 compiles.
RECORDER_JS = "(() => {\n" + HELPERS + r"""
if (window.__lanternRecorder) return;
window.__lanternRecorder = true;
const send = (ev) => {
  try { window.__lanternRecord(Object.assign({url: location.href, title: document.title, ts: Date.now()}, ev)); }
  catch (e) { /* the page outlived the recording */ }
};
const fields = () => H.discover().fields.map(f => {
  const out = {label: f.label, name: f.name, html_id: f.html_id, type: f.type, value: f.value,
               autocomplete: f.autocomplete, placeholder: f.placeholder, aria_label: f.aria_label, captcha: f.captcha};
  if (f.options) out.options = f.options.map(o => ({value: o.value, text: o.text}));
  if (f.type === 'password') out.value = f.value ? '\u2022' : '';   // that it was typed, never what
  return out;
});
document.addEventListener('change', (e) => {
  const el = e.target;
  if (!el || !el.matches || !el.matches('input, select, textarea')) return;
  send({kind: 'field', name: el.getAttribute('name') || '', html_id: el.id || '', type: H.typeOf(el)});
}, true);
document.addEventListener('click', (e) => {
  const b = e.target && e.target.closest && e.target.closest('button, input[type=submit], input[type=image], a, [role=button]');
  if (!b) return;
  send({kind: 'click', tag: b.tagName.toLowerCase(), text: (H.txt(b) || b.value || '').slice(0, 60),
        html_id: b.id || '', name: b.getAttribute('name') || '', type: (b.getAttribute('type') || '').toLowerCase(),
        href: b.getAttribute('href') || '', in_form: !!(b.form || b.closest('form')), fields: fields()});
}, true);
const loaded = () => send({kind: 'page', fields: fields()});
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loaded); else loaded();
})();"""


# S0.5: where the given values show on screen (form controls and text), as boxes in viewport pixels.
ON_SCREEN = r"""
const values = (arg || []).map(v => String(v).trim().toLowerCase()).filter(v => v.length >= 3);
const boxes = [];
if (!values.length) return boxes;
const W = window.innerWidth, Hh = window.innerHeight;
const shows = (t) => { t = String(t || '').toLowerCase(); return values.some(v => t.includes(v)); };
const add = (r) => {
  if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.right < 0 || r.top > Hh || r.left > W) return;
  boxes.push({x: Math.max(0, r.left - 2), y: Math.max(0, r.top - 2), w: r.width + 4, h: r.height + 4});
};
const seen = (el) => { const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
for (const el of document.querySelectorAll('input, textarea, select')) {
  const type = (el.type || '').toLowerCase();
  if (['checkbox', 'radio', 'hidden', 'file', 'submit', 'button', 'image', 'reset'].includes(type) || !seen(el)) continue;
  let shown = el.value;
  if (el.tagName === 'SELECT' && el.selectedIndex >= 0) shown = el.options[el.selectedIndex].text + ' ' + el.value;
  if (shows(shown)) add(el.getBoundingClientRect());
}
const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
for (let n = walker.nextNode(); n; n = walker.nextNode()) {
  const el = n.parentElement;
  if (!el || el.closest('[data-lantern-mask], script, style, noscript, template') || !shows(n.nodeValue) || !seen(el)) continue;
  const range = document.createRange();
  range.selectNodeContents(n);
  for (const r of range.getClientRects()) add(r);
}
return boxes;
"""
PAINT_MASKS = """(boxes) => { for (const b of boxes) {
  const d = document.createElement('div');
  d.setAttribute('data-lantern-mask', '');
  d.style.cssText = `position:fixed;left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;` +
                    'background:#000;z-index:2147483647;pointer-events:none';
  document.documentElement.appendChild(d);
} }"""
CLEAR_MASKS = "() => document.querySelectorAll('[data-lantern-mask]').forEach(e => e.remove())"
# S7 decoy screens: show stand-ins in the page's own elements for one screenshot (same font, size and place), then
# put every real value back. The saved state lives in the returned handle, never on `window`, so a page script can't
# keep Lantern from restoring it.
DECOY_PAINT = r"""(pairs) => {
  const low = pairs.map(([r, f]) => [String(r).toLowerCase(), String(r), String(f)]);
  const swap = (text) => {
    let out = String(text), n = 0;
    for (const [lr, r, f] of low) {
      let i = out.toLowerCase().indexOf(lr);
      while (i >= 0) { out = out.slice(0, i) + f + out.slice(i + r.length); n++; i = out.toLowerCase().indexOf(lr, i + f.length); }
    }
    return [out, n];
  };
  const saved = [];
  let count = 0;
  for (const el of document.querySelectorAll('input, textarea')) {
    const type = (el.type || '').toLowerCase();
    if (['checkbox', 'radio', 'hidden', 'file', 'submit', 'button', 'image', 'reset', 'password'].includes(type)) continue;
    const [v, n] = swap(el.value);
    if (!n) continue;
    let s = null, e = null;
    try { s = el.selectionStart; e = el.selectionEnd; } catch (err) { /* no selection on this type */ }
    saved.push({el, value: el.value, s, e});
    el.value = v;
    count += n;
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
  for (const node of nodes) {
    const p = node.parentElement;
    if (!p || p.closest('script, style, noscript, template')) continue;
    const [t, n] = swap(node.nodeValue);
    if (!n) continue;
    saved.push({node, text: node.nodeValue});
    node.nodeValue = t;
    count += n;
  }
  const fakes = low.map(([, , f]) => f.toLowerCase());
  return {
    count,
    restore() {
      for (const x of saved.reverse()) {
        if (x.node) { x.node.nodeValue = x.text; continue; }
        x.el.value = x.value;
        try { if (x.s !== null) x.el.setSelectionRange(x.s, x.e); } catch (err) { /* not focusable now */ }
      }
      // what's left showing a stand-in (a page script changed it meanwhile): Lantern stops rather than submit it
      return [...document.querySelectorAll('input, textarea')].filter((el) => fakes.some((f) => String(el.value).toLowerCase().includes(f))).length;
    },
  };
}"""


class BrowserDriver:
    """One Chromium session. Use as a context manager."""

    def __init__(self, profile_dir: str | Path, *, headless: bool = True, executable_path: str | None = None,
                 timeout_ms: int = 10_000, guard_submits: bool = True):
        self.profile_dir = Path(profile_dir).expanduser()
        self.headless = headless
        # False only while a person drives the browser (S8 teaching): their own clicks submit as usual.
        self.guard_submits = guard_submits
        self.executable_path = executable_path
        self.timeout_ms = timeout_ms
        self._pw = None
        self._ctx = None
        self.page = None
        self.width, self.height = WIDTH, HEIGHT

    # ---- lifecycle -------------------------------------------------------------
    def __enter__(self) -> "BrowserDriver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise DriverError("Playwright isn't installed (pip install playwright, then "
                              "python -m playwright install chromium on your laptop)") from e
        prepare_profile(self.profile_dir)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, executable_path=self.executable_path,
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1,
                accept_downloads=False, args=LAUNCH_ARGS,
            )
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise DriverError(f"couldn't start Chromium: {str(e).splitlines()[0][:200]}") from e
        if self.guard_submits:
            self._ctx.add_init_script(SUBMIT_GUARD)
        self._ctx.set_default_timeout(self.timeout_ms)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            self._ctx = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    # ---- page --------------------------------------------------------------------
    @property
    def url(self) -> str:
        return self.page.url

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="load")

    def _js(self, body: str, arg: Any = None) -> Any:
        return self.page.evaluate(f"(arg) => {{ {HELPERS}\n{body} }}", arg)

    def discover(self) -> dict:
        return self._js("return H.discover();")

    def field_values(self) -> list[dict]:
        return self.discover()["fields"]

    def hidden_values(self) -> str:
        return self._js("return H.hidden();")

    def captcha_present(self) -> bool:
        return bool(self._js("return H.captchaPresent();"))

    def element_at(self, x: int, y: int) -> dict | None:
        return self._js("const el = document.elementFromPoint(arg[0], arg[1]); return el ? H.describe(el) : null;", [x, y])

    def focused(self) -> dict:
        return self._js("const el = document.activeElement;"
                        "return (!el || el === document.body) ? {tag: 'body'} : H.describe(el);")

    def blocked_submits(self) -> int:
        return int(self.page.evaluate("window.__lanternBlocked || 0"))

    def find_submit(self) -> dict | None:
        return self._js("return H.findSubmit();")

    def scroll_top(self) -> None:
        self.page.evaluate("window.scrollTo(0, 0)")

    def screenshot(self) -> bytes:
        return self.page.screenshot(type="jpeg", quality=JPEG_QUALITY, full_page=False)

    def record(self, callback: Callable[[dict], None]) -> None:
        """S8: call `callback(event)` for what the user does on every page from now on (RECORDER_JS).
        Events arrive while the Playwright loop runs: keep calling `wait()` while the user works."""
        self._ctx.expose_binding("__lanternRecord", lambda _source, event: callback(dict(event or {})))
        self._ctx.add_init_script(RECORDER_JS)

    def confirmation(self, selectors: tuple[str, ...]) -> str:
        """The text of a visible confirmation element on this page, or ''."""
        try:
            el = self.page.query_selector(", ".join(selectors))
            return (el.inner_text() or "").strip()[:300] if el is not None and el.is_visible() else ""
        except Exception:   # the page is closing or navigating
            return ""

    def on_screen(self, values: list[str]) -> list[dict]:
        """Boxes where any of these values shows in the viewport (S0.5: how much a screenshot would expose)."""
        return list(self._js(ON_SCREEN, [str(v) for v in values])) if values else []

    def masked_screenshot(self, values: list[str]) -> tuple[bytes, int]:
        """A screenshot with a black box over every place these values show. Returns (jpeg, boxes painted)."""
        boxes = self.on_screen(values)
        if boxes:
            self.page.evaluate(PAINT_MASKS, boxes)
        try:
            return self.screenshot(), len(boxes)
        finally:
            if boxes:
                self.page.evaluate(CLEAR_MASKS)

    def decoy_screenshot(self, pairs: list[list[str]]) -> tuple[bytes, int]:
        """S7: a screenshot with each real value shown as its stand-in, in the page's own elements; the page is put
        back right after. Returns (jpeg, values replaced). DriverError if a stand-in is still in a field after."""
        if not pairs:
            return self.screenshot(), 0
        handle = self.page.evaluate_handle(DECOY_PAINT, pairs)
        try:
            n = int(handle.evaluate("(h) => h.count"))
            return self.screenshot(), n
        finally:
            left = int(handle.evaluate("(h) => h.restore()"))
            handle.dispose()
            if left:
                raise DriverError("a stand-in value was still in the form after the screenshot; stopped to be safe")

    # ---- filling by DOM ------------------------------------------------------------
    def set_value(self, field: dict, value: str) -> bool:
        """Fill one discovered field. Returns True only if reading it back shows the value."""
        ftype, loc = field["type"], self.page.locator(field["selector"]).first
        try:
            if ftype == "checkbox":
                want = str(value).strip().lower() in ("true", "yes", "y", "on", "1", "checked", "x")
                loc.set_checked(want)
                return loc.is_checked() == want
            if ftype == "radio":
                opt = _match_option(field.get("options") or [], value)
                if not opt:
                    return False
                radio = self.page.locator(opt["selector"]).first
                radio.check()
                return radio.is_checked()
            if ftype == "select":
                opt = _match_option(field.get("options") or [], value)
                if not opt:
                    return False
                loc.select_option(value=opt["value"])
                return loc.input_value() == opt["value"]
            if ftype in ("custom", "file", "frame"):
                return False
            if ftype == "date":
                value = iso_date(value)
            elif ftype == "number":
                value = re.sub(r"[^\d.\-]", "", value)
            loc.fill(value)
            if ftype == "richtext":
                return loc.inner_text().strip() == value.strip()
            return loc.input_value() == value
        except Exception:  # Playwright errors: not editable, detached, timeout
            return False

    def upload(self, field: dict, name: str, mime: str, data: bytes) -> bool:
        loc = self.page.locator(field["selector"]).first
        try:
            loc.set_input_files({"name": name, "mimeType": mime, "buffer": data})
            return loc.evaluate("el => [...el.files].map(f => f.name).join(',')") == name
        except Exception:
            return False

    # ---- raw input (for the computer-use loop) ------------------------------------
    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.page.mouse.click(x, y, button=button, click_count=count)

    def move(self, x: int, y: int) -> None:
        self.page.mouse.move(x, y)

    def drag(self, path: list[tuple[int, int]]) -> None:
        (x0, y0), rest = path[0], path[1:]
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        for x, y in rest:
            self.page.mouse.move(x, y, steps=5)
        self.page.mouse.up()

    def scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.page.mouse.move(x, y)
        self.page.mouse.wheel(dx, dy)

    def type_text(self, text: str) -> None:
        self.page.keyboard.type(text, delay=5)

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def settle(self) -> None:
        """Give the page a moment to react to the last input."""
        try:
            self.page.wait_for_load_state("load", timeout=5_000)
        except Exception:
            pass
        self.page.wait_for_timeout(120)

    # ---- submit + verify -------------------------------------------------------------
    def submit(self, selector: str) -> None:
        self.page.evaluate("window.__lanternSubmitAllowed = true")
        self.page.locator(selector).first.click()
        self.settle()

    def verify_submission(self, before_url: str, selectors: tuple[str, ...],
                          wait_ms: int = 4_000) -> tuple[bool, str]:
        """Evidence that the submission went through: a confirmation element on the page,
        or (weaker) a new page with no form and a thank-you / reference message."""
        sel = ", ".join(selectors)
        try:
            self.page.wait_for_selector(sel, state="visible", timeout=wait_ms)
        except Exception:
            pass
        el = self.page.query_selector(sel)
        if el is not None and el.is_visible():
            return True, (el.inner_text() or "").strip()[:300]
        if self.page.url != before_url:
            body = self.page.inner_text("body")[:2000]
            no_form = self.page.query_selector("form input:not([type=hidden]), form textarea") is None
            if no_form and re.search(r"thank you|received|submitted|success|reference|confirmation", body, re.I):
                return True, body.strip()[:300]
        return False, ""


def _match_option(options: list[dict], value: str) -> dict | None:
    v = str(value).strip().lower()
    for key in ("text", "value"):
        for o in options:
            if str(o.get(key, "")).strip().lower() == v:
                return o
    for o in options:  # "Travel (flights, trains)" matches "travel"
        if v and v in str(o.get("text", "")).lower():
            return o
    return None
