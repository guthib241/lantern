# Lantern desktop

The face of Lantern on your laptop: an always-on-top avatar, plus a panel for tasks, approvals, privacy and costs.

```bash
cd desktop
npm install          # Electron 44.5.1
npm run mock         # no Python, no API key: a scripted story (form → approval → verified)
npm start            # the real thing: needs `python -m lantern --serve` running
                     # (or `python -m lantern --serve --demo`: real server, scripted model, $0)
npm test             # logic + mock server tests (node --test)
```

- **Avatar window:** transparent, frameless, always on top, and click-through except over the avatar. Click it to open the panel; right-click it for STOP and Quit. The face follows Lantern's state (idle, thinking, smug, annoyed, celebrating, needs-approval, sleeping). Faces come from Muse: `assets/avatar/<expression>.webm` (animated, Muse Video / Vibes), `.webp` or `.png` (Muse Image). A faceless glow stands in until they exist.
- **Panel:**
  - Approval cards with one big **Allow** button, warnings, "left for you", and the payload hash each card is bound to.
  - Tasks with a **model badge** on every step.
  - **What left my laptop today** and the privacy-zones editor.
  - A simulated WhatsApp phone, for rehearsals.
  - Spend by model.
  - Sandboxed widgets.
- **STOP:** the button, or **Ctrl/Cmd+Shift+X** anywhere.
- **Security:** renderers run sandboxed with context isolation and no Node. A strict CSP allows only `127.0.0.1` connections. No navigation, no popups, no device permissions. Server text is always shown as text. The token comes from `~/.lantern/token`. Widgets run in `sandbox="allow-scripts"` frames inside `widget-host.html` (`default-src 'none'`), so they get no network and no access to the panel.
