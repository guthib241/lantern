/* Lantern phone: chat-only PWA. Plain JS, same-origin API, no CDN. */
(function () {
  "use strict";
  var LS_TOKEN = "lantern_device";
  var LS_ID = "lantern_device_id";

  function $(id) { return document.getElementById(id); }
  function device() { return localStorage.getItem(LS_TOKEN) || ""; }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  async function api(path, opts) {
    var tok = device();
    var headers = { "Content-Type": "application/json" };
    if (tok) headers["X-Lantern-Device"] = tok;
    var res = await fetch(path, Object.assign({ headers: headers }, opts || {}));
    if (res.status === 401) {
      unpair(true);
      throw new Error("This phone is unpaired. Pair it again from the desktop.");
    }
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new Error((data && (data.detail || data.message)) || ("Request failed (" + res.status + ")"));
    return data;
  }

  function show(which) {
    $("screen-pair").hidden = which !== "pair";
    $("screen-chat").hidden = which !== "chat";
    $("composer").hidden = which !== "chat";
  }

  function unpair(silent) {
    localStorage.removeItem(LS_TOKEN);
    localStorage.removeItem(LS_ID);
    stopSocket();
    show("pair");
    $("status").textContent = "not paired";
    if (!silent) $("pair-code").focus();
  }

  // ---- pairing -------------------------------------------------------------
  $("pair-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    var code = $("pair-code").value.trim();
    var name = $("pair-name").value.trim() || "My phone";
    $("pair-error").textContent = "";
    if (!/^[0-9]{6}$/.test(code)) {
      $("pair-error").textContent = "Type the 6 digits shown on the desktop.";
      return;
    }
    try {
      var res = await fetch("/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code, name: name }),
      });
      var data = null;
      try { data = await res.json(); } catch (err) { data = null; }
      if (!res.ok) throw new Error((data && (data.detail || data.message)) || "That code did not work.");
      localStorage.setItem(LS_TOKEN, data.device_token);
      localStorage.setItem(LS_ID, data.device_id);
      $("pair-code").value = "";
      show("chat");
      boot();
    } catch (err) {
      $("pair-error").textContent = err.message;
    }
  });

  // ---- chat -----------------------------------------------------------------
  var lastIds = [];
  var sending = false;

  function renderThread(tasks) {
    var thread = $("thread");
    thread.textContent = "";
    $("empty").hidden = tasks.length > 0;
    var list = tasks.slice().reverse();
    for (var i = 0; i < list.length; i++) {
      (function (t) {
        var wrap = el("div", "task");
        wrap.appendChild(taskBubble("me", t.goal || ""));
        var them = el("div", "them");
        them.appendChild(taskBubble("bubble", t.reply || (t.status === "active" ? "Working on it…" : "")));
        var steps = t.steps || [];
        if (steps.length) {
          var det = document.createElement("details");
          det.className = "steps";
          var sum = document.createElement("summary");
          sum.textContent = "Steps (" + steps.length + ")";
          det.appendChild(sum);
          var ul = document.createElement("ul");
          steps.slice(0, 12).forEach(function (s) {
            ul.appendChild(el("li", null, s.text || s.kind));
          });
          det.appendChild(ul);
          them.appendChild(det);
        }
        var st = el("div", "status", "#" + t.id + " · " + t.status);
        them.appendChild(st);
        wrap.appendChild(them);
        thread.appendChild(wrap);
      })(list[i]);
    }
    window.scrollTo(0, document.body.scrollHeight);
  }

  function taskBubble(kind, text) {
    var side = el("div", kind === "me" ? "me" : null);
    side.appendChild(el("div", "bubble", text));
    if (kind === "me") {
      var wrap = el("div", "task");
      wrap.appendChild(side);
      return wrap;
    }
    return side;
  }

  async function refreshTasks() {
    if (!device()) return;
    try {
      var tasks = await api("/m/api/tasks?limit=10");
      lastIds = tasks.map(function (t) { return t.id; });
      renderThread(tasks);
      var active = tasks.some(function (t) { return t.status === "active"; });
      $("working").hidden = !active && !sending;
      $("status").textContent = active ? "working…" : "idle";
    } catch (err) {
      $("status").textContent = "offline";
    }
  }

  $("ask").addEventListener("submit", async function (e) {
    e.preventDefault();
    var box = $("goal");
    var goal = box.value.trim();
    if (!goal || sending) return;
    sending = true;
    box.value = "";
    $("working").hidden = false;
    $("empty").hidden = true;
    try {
      await api("/m/api/task", { method: "POST", body: JSON.stringify({ goal: goal }) });
      await refreshTasks();
      setTimeout(refreshTasks, 1500);
      setTimeout(refreshTasks, 4000);
    } catch (err) {
      $("status").textContent = err.message;
    } finally {
      sending = false;
    }
  });

  // ---- approvals (sheet) ------------------------------------------------------
  var current = null;
  var polling = false;

  function showSheet(a) {
    current = a;
    $("sheet-title").textContent = a.title || "Needs your OK";
    var facts = $("sheet-facts");
    facts.textContent = "";
    var flags = (a.flags || []).filter(Boolean);
    // the card's warning and flag lines are shown once, in the yellow box below
    var lines = String(a.text || "").split("\n").slice(1).filter(function (line) {
      return line.trim() && line.indexOf("\u26a0") !== 0 && flags.indexOf(line.trim()) < 0;
    }).slice(0, 6);
    lines.forEach(function (line) { facts.appendChild(el("p", null, line)); });
    var untrusted = $("sheet-untrusted");
    if (a.tainted || flags.length) {
      untrusted.hidden = false;
      untrusted.textContent = "";
      untrusted.appendChild(el("div", null, "Comes from a page or file, not from you."));
      flags.forEach(function (f) { untrusted.appendChild(el("div", null, f)); });
    } else {
      untrusted.hidden = true;
    }
    var mins = a.expires_in || "?";
    $("sheet-meta").textContent = "Expires in " + mins + " min · #" + a.id;
    $("sheet").hidden = false;
    $("sheet-bg").hidden = false;
  }

  function hideSheet() {
    current = null;
    $("sheet").hidden = true;
    $("sheet-bg").hidden = true;
  }

  async function decide(approve) {
    if (!current) return;
    var a = current;
    var allowBtn = $("sheet-allow");
    var denyBtn = $("sheet-deny");
    allowBtn.disabled = true;
    denyBtn.disabled = true;
    try {
      await api("/m/api/approvals/" + encodeURIComponent(a.id), {
        method: "POST",
        body: JSON.stringify({ nonce: a.nonce, approve: approve, expected_hash: a.payload_hash || undefined }),
      });
      hideSheet();
      pollApprovals();
      refreshTasks();
    } catch (err) {
      $("sheet-meta").textContent = err.message;
    } finally {
      allowBtn.disabled = false;
      denyBtn.disabled = false;
    }
  }

  $("sheet-allow").addEventListener("click", function () { decide(true); });
  $("sheet-deny").addEventListener("click", function () { decide(false); });
  $("sheet-bg").addEventListener("click", hideSheet);

  async function pollApprovals() {
    if (!device() || polling) return;
    polling = true;
    try {
      var items = await api("/m/api/approvals");
      if (items.length && !current) showSheet(items[0]);
      if (!items.length && current) hideSheet();
      if (items.length && current && items[0].id !== current.id) showSheet(items[0]);
    } catch (err) { /* offline: keep the old sheet */ }
    polling = false;
  }

  // ---- live events with backoff ------------------------------------------------
  var ws = null;
  var backoff = 1000;
  var wsTimer = null;

  function stopSocket() {
    if (ws) { try { ws.close(); } catch (e) { /* closed */ } ws = null; }
    if (wsTimer) { clearTimeout(wsTimer); wsTimer = null; }
  }

  function connectSocket() {
    if (!device() || ws) return;
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host +
      "/m/events?device=" + encodeURIComponent(device());
    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    ws.onopen = function () {
      backoff = 1000;
      setOnline(false);
      $("status").textContent = "connected";
    };
    ws.onmessage = function (ev) {
      var msg = null;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || !msg.type) return;
      if (msg.type === "hello" && msg.pending && msg.pending.length && !current) showSheet(msg.pending[0]);
      else if (msg.type === "approval" && msg.event === "requested" && msg.approval) showSheet(msg.approval);
      else if (msg.type === "approval" && msg.event !== "requested") { hideSheet(); pollApprovals(); }
      else if (msg.type === "done" || msg.type === "queued" || msg.type === "stopped") refreshTasks();
      else if (msg.type === "reminder") refreshReminders();
    };
    ws.onclose = function () { ws = null; scheduleReconnect(); };
    ws.onerror = function () { try { ws.close(); } catch (e) { /* retry */ } };
  }

  function scheduleReconnect() {
    if (!device()) return;
    setOnline(true);
    wsTimer = setTimeout(function () {
      wsTimer = null;
      backoff = Math.min(backoff * 2, 30000);
      connectSocket();
      pollApprovals();
      refreshTasks();
    }, backoff);
  }

  function setOnline(offline) {
    $("offline").hidden = !offline && navigator.onLine;
  }
  window.addEventListener("online", function () { setOnline(false); connectSocket(); });
  window.addEventListener("offline", function () { setOnline(true); });

  // ---- menu: reminders, stop, unpair -------------------------------------------
  var menu = $("menu");
  $("menu-btn").addEventListener("click", function () {
    var open = menu.hidden;
    menu.hidden = !open;
    $("menu-btn").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", function (e) {
    if (!menu.hidden && !$("menu-btn").contains(e.target) && !menu.contains(e.target)) menu.hidden = true;
  });

  $("menu-reminders").addEventListener("click", async function () {
    menu.hidden = true;
    var box = $("reminders-box");
    box.hidden = !box.hidden;
    if (!box.hidden) refreshReminders();
  });

  async function refreshReminders() {
    var list = $("reminders");
    list.textContent = "";
    try {
      var items = await api("/m/api/reminders");
      $("reminders-box").hidden = false;
      if (!items.length) list.appendChild(el("li", null, "No reminders pending."));
      items.forEach(function (r) {
        var li = el("li", null, (r.text || "") + " · " + (r.due || ""));
        var btn = el("button", null, "Cancel");
        btn.type = "button";
        btn.addEventListener("click", async function () {
          try {
            await api("/m/api/reminders/" + encodeURIComponent(r.id) + "/cancel", { method: "POST" });
            refreshReminders();
          } catch (err) { $("status").textContent = err.message; }
        });
        li.appendChild(btn);
        list.appendChild(li);
      });
    } catch (err) { $("status").textContent = err.message; }
  }

  $("menu-stop").addEventListener("click", async function () {
    menu.hidden = true;
    if (!window.confirm("Stop everything running?")) return;
    try {
      await api("/m/api/stop", { method: "POST" });
      refreshTasks();
    } catch (err) { $("status").textContent = err.message; }
  });

  $("menu-unpair").addEventListener("click", function () {
    menu.hidden = true;
    if (!window.confirm("Unpair this phone? You can pair it again from the desktop.")) return;
    unpair();
  });

  $("face").addEventListener("error", function () {
    $("face").style.display = "none";
  });

  // ---- boot ---------------------------------------------------------------------
  function boot() {
    if (!device()) { unpair(true); return; }
    show("chat");
    refreshTasks();
    pollApprovals();
    connectSocket();
    setInterval(pollApprovals, 5000);
    setInterval(function () { if (!ws) refreshTasks(); }, 8000);
  }

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/m/sw.js").catch(function () { /* optional */ });
    });
  }

  boot();
})();
