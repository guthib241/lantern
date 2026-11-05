"""The routine runtime (S4): the only API a compiled routine may call.

    from lantern.routines.runtime import browser, check, files, step, vault

    def run(params):
        with step("s1", "Open the personal details page"):
            browser.open(params["base_url"] + "/apply/personal")
            check.url_contains("/apply/personal")

The names are bound per run to a `RoutineContext`, which holds the rules, in code:
  - Every browser target is cross-checked in the page before it is used: a visible, enabled control in the
    page itself (not in a frame), whose label matches. No match means the step fails (and may heal),
    never a guess.
  - Password, one-time code, card and ID fields are hard stops (forms/guards.py): a routine never fills them.
  - Files only through policy.py (privacy zones, writable zones, no symlinks); private files never upload.
  - Red actions (submit, move) run only inside the plan the user approved for this run: one approval, bound
    to the hash of every value, checked again in code before each red action.
  - Modes: live (for real, with that approval), sandbox (a throwaway target, e.g. a fresh demo portal or a
    temporary folder; no approval needed), dry (no red action happens; their checks are skipped and
    reported as such).
  - uia.* (Windows UI Automation) is laptop-only (LANTERN_UIA=on); everywhere else it fails clearly.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from ..forms import guards
from ..policy import PRIVATE, PolicyError

MODES = ("live", "sandbox", "dry")


class DryStop(Exception):
    """A dry run reached its first red action: everything before it was checked, nothing after it can be
    (a multi-page form's next page only exists after a real submit). Not a failure."""

    def __init__(self, step_id: str | None):
        super().__init__(step_id)
        self.step_id = step_id


class StepFailed(Exception):
    """A step couldn't be done or its check didn't hold. kind: action | postcondition | hard_stop |
    missing_value | refused | not_available."""

    def __init__(self, step_id: str | None, why: str, kind: str = "action", action: str = ""):
        super().__init__(why)
        self.step_id, self.why, self.kind, self.action = step_id, why, kind, action


def sha(value: Any) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


BUTTONS_JS = """() => {
  const vis = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')].filter(vis);
  return els.map((e, i) => { e.setAttribute('data-lantern-btn', String(i));
    return {i, label: (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim(),
            disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true',
            submit: (e.tagName === 'BUTTON' && e.type === 'submit') || e.matches('input[type=submit]'),
            html_id: e.id || '', name: e.getAttribute('name') || '',
            in_form: !!(e.form || e.closest('form'))}; });
}"""


class RoutineContext:
    def __init__(self, *, mode: str = "live", policy=None, vault: dict | None = None,
                 driver_factory: Callable[[], Any] | None = None, url_ok: Callable[[str], str | None] | None = None,
                 approve: Callable[[str, str, dict], Any] | None = None, plan: dict | None = None,
                 heal: Callable[..., str | None] | None = None, on_moved: Callable[[list[dict]], None] | None = None,
                 emit: Callable[[str, dict], None] | None = None, cancel=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.policy = policy
        self.vault_data = dict(vault or {})
        self.driver_factory = driver_factory
        self.url_ok = url_ok or (lambda url: None)
        self.approve = approve            # live: asks once for the whole plan (gate.ask / Toolbox._approve)
        self.plan = plan or {}            # {"payload": {...}, "summary": str, "red": [step ids], "values": {sid: sha}}
        self.heal = heal                  # (step_id, kind, old_target, candidates) -> new target or None
        self.on_moved = on_moved or (lambda moves: None)
        self.on_red = None                # called once, right before the first red action happens
        self.emit = emit or (lambda kind, data: None)
        self.cancel = cancel
        self.log: list[dict] = []         # one record per step
        self.patches: dict[str, dict] = {}  # step id -> {"field"|"button": healed target}
        self.filled: dict[str, str] = {}  # step id -> sha of the value filled (checked against the plan)
        self.current: str | None = None
        self.acted = False                # a red action happened in this run
        self.skipped: set[str] = set()    # steps whose red action was skipped (dry run)
        self.sorted: set[str] = set()     # files.sort: files a rule already took (each file moves once)
        self.moved: list[dict] = []       # files.sort: what moved, checked on disk
        self.left: list[str] = []         # files.sort: files that weren't on the approved list
        self._commit = None
        self._committed = False
        self._driver = None
        self.browser, self.files, self.vault = Browser(self), Files(self), Vault(self)
        self.check, self.uia = Checks(self), UIA(self)

    # ---- the names a routine imports ----------------------------------------------------
    def names(self) -> dict:
        return {"browser": self.browser, "files": self.files, "vault": self.vault, "check": self.check,
                "uia": self.uia, "step": self.step}

    @contextmanager
    def step(self, step_id: str, say: str = ""):
        if self.cancel is not None and self.cancel.is_set():
            raise StepFailed(step_id, "stopped by the user", "refused")
        rec = {"id": step_id, "say": say, "ok": False, "seconds": 0.0}
        self.current, t0 = step_id, time.monotonic()
        try:
            yield
        except DryStop:
            rec["ok"], rec["dry_skipped"] = True, True
            raise
        except StepFailed as e:
            e.step_id = e.step_id or step_id
            rec["error"], rec["kind"] = e.why, e.kind
            raise
        except Exception as e:   # a runtime bug or a browser error: the step failed, with the reason
            rec["error"], rec["kind"] = f"{type(e).__name__}: {str(e)[:200]}", "action"
            raise StepFailed(step_id, rec["error"]) from e
        else:
            rec["ok"] = True
        finally:
            rec["seconds"] = round(time.monotonic() - t0, 3)
            if step_id in self.patches:
                rec["healed"] = self.patches[step_id]
            self.log.append(rec)
            self.emit("routine_step", rec)
            self.current = None

    def fail(self, why: str, kind: str = "action", action: str = "") -> StepFailed:
        return StepFailed(self.current, why, kind, action)

    # ---- the browser, opened on first use in this thread ------------------------------------
    @property
    def driver(self):
        if self._driver is None:
            if self.driver_factory is None:
                raise self.fail("no browser is available here", "not_available")
            d = self.driver_factory()
            d.__enter__()
            self._driver = d
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.__exit__(None, None, None)
            finally:
                self._driver = None

    # ---- red actions: inside the approved plan, or not at all ---------------------------------
    def red(self, action: str) -> bool:
        """True if the red action may happen now. A dry run stops here (DryStop). Raises if outside the plan."""
        sid = self.current
        if self.mode == "dry":
            self.skipped.add(sid)
            raise DryStop(sid)
        if self.mode == "sandbox":
            return True
        if sid not in set(self.plan.get("red") or []):
            raise self.fail(f"{action} in step {sid} isn't in the plan you approved", "refused", action)
        for filled_sid, digest in self.filled.items():
            if self.plan.get("values", {}).get(filled_sid) not in (None, digest):
                raise self.fail(f"the value in step {filled_sid} differs from the one you approved", "refused", action)
        if not self._committed:
            if self._commit is None:
                raise self.fail("the run wasn't approved", "refused", action)
            try:
                self._commit(self.plan["payload"])   # bound to the hash of exactly what was shown
            except Exception as e:
                raise self.fail(f"the approval can't be used: {e}", "refused", action) from e
            self._committed = True
        if self.on_red is not None and not self.acted:
            self.on_red()
        return True

    def ask_approval(self) -> bool:
        """Live runs: one approval for the whole plan before anything happens."""
        if self.mode != "live" or not self.plan.get("red"):
            return True
        if self.approve is None:
            return False
        self._commit = self.approve("run_routine", self.plan["summary"], self.plan["payload"])
        return self._commit is not None


class Browser:
    def __init__(self, ctx: RoutineContext):
        self._ctx = ctx

    def open(self, url: str) -> None:
        url = str(url)
        if not re.match(r"^https?://", url):
            raise self._ctx.fail(f"not a web address: {url[:80]}")
        if problem := self._ctx.url_ok(url):
            raise self._ctx.fail(problem, "refused")
        self._ctx.driver.open(url)

    def _field(self, label: str, kind: str = "field", **hints) -> dict:
        ctx, d = self._ctx, self._ctx.driver
        label = ctx.patches.get(ctx.current, {}).get(kind, label)
        fields = d.discover()["fields"]
        found = _match(fields, label, hints)
        if found is None and ctx.heal is not None:
            new = ctx.heal(ctx.current, kind, label, [{k: f.get(k, "") for k in ("label", "name", "html_id", "type")}
                                                      for f in fields], page=self._page_info())
            if new:
                found = _match(fields, new, {})
                if found is not None:
                    ctx.patches.setdefault(ctx.current, {}).update(
                        {kind: found["label"] or new, "name": found.get("name", ""), "html_id": found.get("html_id", "")})
        if found is None:
            raise ctx.fail(f"no field labelled '{label}' on this page")
        if reason := guards.classify(found):
            raise ctx.fail(f"'{found['label']}' is a field only you may fill ({reason})", "hard_stop")
        return found

    def fill(self, field: str, value: str, **hints) -> None:
        value = str(value)
        if reason := guards.value_reason(value):
            raise self._ctx.fail(f"Lantern never types that kind of value ({reason})", "hard_stop")
        f = self._field(field, **hints)
        if f["type"] in ("file", "frame"):
            raise self._ctx.fail(f"'{f['label']}' takes a file; use browser.upload")
        if not self._ctx.driver.set_value(f, value):
            raise self._ctx.fail(f"couldn't set '{f['label']}' (the page didn't keep the value)")
        self._ctx.filled[self._ctx.current] = sha(value)

    def upload(self, field: str, path: str, **hints) -> None:
        ctx = self._ctx
        if ctx.policy is None:
            raise ctx.fail("file access isn't set up", "not_available")
        try:
            p, zone = ctx.policy.check_visible(path)
        except PolicyError as e:
            raise ctx.fail(str(e), "refused") from e
        if zone == PRIVATE:
            raise ctx.fail("that file is in a private zone; private files never leave this computer", "hard_stop")
        if not p.is_file():
            raise ctx.fail(f"{p.name} is not a file", "missing_value")
        f = self._field(field, **hints)
        data = ctx.policy.read_bytes(p)
        import mimetypes
        if not ctx.driver.upload(f, p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream", data):
            raise ctx.fail(f"the page didn't accept {p.name}")
        ctx.filled[ctx.current] = sha(path)

    def _page_info(self) -> dict:
        """The page's title and main heading: context for a repair (untrusted page text, kept short)."""
        try:
            info = self._ctx.driver.page.evaluate(
                "() => ({title: document.title || '', heading: (document.querySelector('h1') || {}).innerText || ''})")
            return {k: " ".join(str(info.get(k, "")).split())[:120] for k in ("title", "heading")}
        except Exception:
            return {}

    def _buttons(self) -> list[dict]:
        return self._ctx.driver.page.evaluate(BUTTONS_JS)

    def submit(self, button: str) -> None:
        ctx = self._ctx
        label = ctx.patches.get(ctx.current, {}).get("button", button)
        buttons = [b for b in self._buttons() if not b["disabled"] and b["label"]]
        found = next((b for b in buttons if norm(b["label"]) == norm(label)), None)
        if found is None and ctx.heal is not None:
            new = ctx.heal(ctx.current, "button", label,
                           [{k: b.get(k) for k in ("label", "html_id", "submit", "in_form") if b.get(k) not in (None, "")}
                            for b in buttons], page=self._page_info())
            found = next((b for b in buttons if norm(b["label"]) == norm(new or "")), None)
            if found is not None:
                ctx.patches.setdefault(ctx.current, {})["button"] = found["label"]
        if found is None:
            raise ctx.fail(f"no enabled button labelled '{label}' on this page")
        ctx.red("submit")   # a dry run stops here: everything up to it was checked, nothing is sent
        d = ctx.driver
        d.page.evaluate("window.__lanternSubmitAllowed = true")
        d.page.locator(f'[data-lantern-btn="{found["i"]}"]').first.click()
        ctx.acted = True
        if hasattr(d, "settle"):
            d.settle()

    def url(self) -> str:
        return self._ctx.driver.url

    def text(self) -> str:
        return self._ctx.driver.page.inner_text("body")


def _match(fields: list[dict], label: str, hints: dict) -> dict | None:
    want = norm(label)
    for key in ("label", "name", "html_id"):
        for f in fields:
            if want and norm(f.get(key, "")) == want:
                return f
    for key in ("name", "html_id"):   # the label changed but the field's own name/id still match
        if hints.get(key):
            for f in fields:
                if f.get(key) and f.get(key) == hints[key]:
                    return f
    return None


def sort_moves(policy, folder: str, match: str, to: str, taken: set[str] | None = None) -> list[tuple[str, str]]:
    """S5: the moves a files.sort step would make now: top-level files in `folder` (never folders, shortcuts or
    hidden files) whose name matches `match`, into `to` (relative to the folder, or a full path). Files another rule
    already took, or whose new name is taken, stay where they are. The plan and the run both use this."""
    base = policy.check_writable(folder)
    target = Path(to) if Path(to).is_absolute() else base / to
    out = []
    for p in policy.list_dir(base):
        if (not p.is_file() or p.name.startswith(".") or policy.is_link(p) or str(p) in (taken or set())
                or not fnmatch.fnmatch(p.name.lower(), match.lower())):
            continue
        dst = target / p.name
        if not dst.exists():
            out.append((str(p), str(dst)))
    return out


class Files:
    def __init__(self, ctx: RoutineContext):
        self._ctx = ctx

    def sort(self, folder: str, match: str, to: str) -> None:
        """Move what matches into `to`: only moves on the approved list, each checked on disk and kept for Undo."""
        ctx = self._ctx
        if ctx.policy is None:
            raise ctx.fail("file access isn't set up", "not_available")
        try:
            moves = sort_moves(ctx.policy, folder, match, to, ctx.sorted)
        except PolicyError as e:
            raise ctx.fail(str(e), "refused") from e
        approved = {tuple(m) for m in ctx.plan.get("moves") or []}
        for src, dst in moves:
            ctx.sorted.add(src)
            if ctx.mode == "live" and (src, dst) not in approved:
                ctx.left.append(src)   # it arrived after you approved: it stays where it is
                continue
            ctx.red("move")
            ctx.policy.move(src, dst)
            ctx.acted = True
            if not Path(dst).is_file() or Path(src).exists():
                raise ctx.fail(f"{Path(src).name} isn't in {to} after the move")
            ctx.moved.append({"from": src, "to": dst})
            ctx.on_moved([{"source": dst, "destination": src}])

    def move(self, source: str, destination: str) -> None:
        ctx = self._ctx
        if ctx.policy is None:
            raise ctx.fail("file access isn't set up", "not_available")
        try:
            src = ctx.policy.check_writable(source)
            dst = ctx.policy.check_writable(destination)
        except PolicyError as e:
            raise ctx.fail(str(e), "refused") from e
        if ctx.policy.is_link(source):
            raise ctx.fail(f"{source} is a shortcut (symlink); Lantern never moves those", "refused")
        if dst.is_dir() or str(destination).endswith(("/", "\\")):
            dst = dst / src.name
        if not src.exists():
            raise ctx.fail(f"{src.name} isn't there any more", "missing_value")
        if dst.exists():
            raise ctx.fail(f"{dst} already exists", "refused")
        ctx.red("move")
        ctx.policy.move(src, dst)
        ctx.acted = True
        if dst.exists() and not src.exists():
            ctx.on_moved([{"source": str(dst), "destination": str(src)}])


class Vault:
    def __init__(self, ctx: RoutineContext):
        self._ctx = ctx

    def get(self, key: str) -> str:
        data = self._ctx.vault_data
        for k in (key, key.replace("_", " ")):
            if str(data.get(k, "")).strip():
                return str(data[k])
        raise self._ctx.fail(f"your profile has no '{key.replace('_', ' ')}'; add it (lantern --profile-set)",
                             "missing_value")


class Checks:
    """Postconditions. In a dry run, checks in a step whose red action was skipped are skipped too."""

    def __init__(self, ctx: RoutineContext):
        self._ctx = ctx

    def _skip(self) -> bool:
        return self._ctx.mode == "dry" and self._ctx.current in self._ctx.skipped

    def _fail(self, why: str):
        raise self._ctx.fail(why, "postcondition")

    def url_contains(self, text: str) -> None:
        if not self._skip() and str(text) not in self._ctx.driver.url:
            self._fail(f"expected the address to contain '{text}', it is {self._ctx.driver.url}")

    def text_contains(self, text: str) -> None:
        if not self._skip() and norm(text) not in norm(self._ctx.browser.text()):
            self._fail(f"expected '{text[:60]}' on the page")

    def text_matches(self, pattern: str) -> None:
        if not self._skip() and not re.search(pattern, self._ctx.browser.text()):
            self._fail(f"expected text matching {pattern[:60]} on the page")

    def field_equals(self, field: str, value: str) -> None:
        if self._skip():
            return
        f = self._ctx.browser._field(field)
        if norm(f.get("value", "")) != norm(value):
            self._fail(f"'{f['label']}' holds '{str(f.get('value', ''))[:40]}', not the value set")

    def file_exists(self, path: str) -> None:
        if not self._skip() and not self._ctx.policy.exists(path):
            self._fail(f"{path} isn't there")

    def file_missing(self, path: str) -> None:
        if not self._skip() and self._ctx.policy.exists(path):
            self._fail(f"{path} is still there")


class UIA:
    """Windows UI Automation: real only on the laptop (LANTERN_UIA=on, Windows); a clear failure elsewhere."""

    def __init__(self, ctx: RoutineContext):
        self._ctx = ctx

    def _unavailable(self, *a, **kw):
        if os.environ.get("LANTERN_UIA", "off").lower() != "on" or os.name != "nt":
            raise self._ctx.fail("desktop app steps need Lantern on the Windows laptop (LANTERN_UIA=on)",
                                 "not_available")
        raise self._ctx.fail("UI Automation steps aren't built yet (S8)", "not_available")

    find = click = type = _unavailable
