"""Make the README's generated media (docs/media/): the animated banner (light and dark), the animated terminal
of `lantern --demo-run`, and the 3D enclosure from the FreeCAD demo as an ASCII STL.

    python scripts/make_readme_media.py --demo-run-log demo_run.txt
    (demo_run.txt: the output of `lantern --demo-run`; the terminal shows lines from it, shortened, never invented)

Everything here is drawn from plain shapes and text: our own visual identity, no logos.
"""
from __future__ import annotations

import argparse
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape

MEDIA = Path(__file__).resolve().parent.parent / "docs" / "media"
SANS = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"


# ---- banner ---------------------------------------------------------------------------------------------------------
def banner(theme: str) -> str:
    dark = theme == "dark"
    bg, ink, sub = ("#15120F", "#F6EFE5", "#B9AFA3") if dark else ("#FAF6EF", "#1B1712", "#6B6157")
    glow_bg = ".30" if dark else ".22"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="380" viewBox="0 0 1200 380" role="img"
  aria-label="Lantern: the AI that lives on your laptop, does the boring computer work, and asks before anything leaves.">
  <defs>
    <radialGradient id="room" cx="17%" cy="50%" r="55%">
      <stop offset="0" stop-color="#FF9F0A" stop-opacity="{glow_bg}"/><stop offset="1" stop-color="#FF9F0A" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="sphere" cx="44%" cy="36%" r="70%">
      <stop offset="0" stop-color="#FFF1C2"/><stop offset=".45" stop-color="#FFC861"/>
      <stop offset=".8" stop-color="#FF9F43"/><stop offset="1" stop-color="#E8730F"/>
    </radialGradient>
    <radialGradient id="halo" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#FFB347" stop-opacity=".75"/><stop offset=".55" stop-color="#FFB347" stop-opacity=".18"/>
      <stop offset="1" stop-color="#FFB347" stop-opacity="0"/>
    </radialGradient>
    <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="10"/></filter>
    <style>
      .float {{ animation: float 6s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
      .breathe {{ animation: breathe 4.5s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
      .light {{ animation: drift 9s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
      @keyframes float {{ 0%, 100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-6px); }} }}
      @keyframes breathe {{ 0%, 100% {{ opacity: .65; transform: scale(.94); }} 50% {{ opacity: 1; transform: scale(1.06); }} }}
      @keyframes drift {{ 0%, 100% {{ transform: translate(-6px, 4px); }} 50% {{ transform: translate(8px, -6px); }} }}
      @media (prefers-reduced-motion: reduce) {{ .float, .breathe, .light {{ animation: none; }} }}
    </style>
  </defs>
  <rect width="1200" height="380" rx="28" fill="{bg}"/>
  <rect width="1200" height="380" rx="28" fill="url(#room)"/>
  <g class="float">
    <circle class="breathe" cx="196" cy="190" r="118" fill="url(#halo)"/>
    <circle cx="196" cy="190" r="70" fill="url(#sphere)"/>
    <ellipse class="light" cx="182" cy="168" rx="34" ry="22" fill="#FFF6DA" opacity=".55" filter="url(#soft)"/>
    <ellipse cx="176" cy="160" rx="20" ry="12" fill="#FFFFFF" opacity=".35"/>
  </g>
  <text x="330" y="172" font-family="{SANS}" font-size="92" font-weight="700" letter-spacing="-3" fill="{ink}">Lantern</text>
  <text x="334" y="226" font-family="{SANS}" font-size="27" fill="{sub}">The AI that lives on your laptop, does the boring computer work,</text>
  <text x="334" y="262" font-family="{SANS}" font-size="27" fill="{sub}">and asks before anything leaves it.</text>
  <text x="334" y="318" font-family="{SANS}" font-size="17" font-weight="600" letter-spacing="2.5" fill="#E8890C">FORMS · EMAIL · FILES · PRIVATE BY DESIGN · BUILT ON META'S MUSE MODELS</text>
</svg>
"""


# ---- terminal ---------------------------------------------------------------------------------------------------------
KEEP = [
    r"^🏮 Rehearsal mode", r"^📱 voice note", r"searched files", r"click \(429, 313\)", r"needs your OK: Submit",
    r"approved on the phone", r"form submitted and verified", r"needs your OK: Send email", r"approved on the phone",
    r"email sent, found in Sent", r"^🏮 Form submitted", r"^💻 you › What base salary", r"asked the private lane",
    r"^🏮 From the private file", r"^📋 Audit", r"^💸 Cost",
]


def terminal_lines(log: str) -> list[str]:
    lines = [l.rstrip() for l in log.splitlines()]
    out, i = [], 0
    for pat in KEEP:
        while i < len(lines) and not re.search(pat, lines[i]):
            i += 1
        if i == len(lines):
            raise SystemExit(f"demo-run log has no line matching {pat!r}")
        text = re.sub(r"Data in \S+", "", lines[i]).rstrip(". ")
        out.append(text if len(text) <= 92 else text[:91] + "…")
        i += 1
    return out


def terminal(lines: list[str]) -> str:
    step, start, lh = 0.55, 1.2, 25
    cycle = start + step * len(lines) + 6.0
    h = 92 + lh * (len(lines) + 1) + 20
    css, rows = [], []
    for n, text in enumerate(lines):
        at = (start + n * step) / cycle * 100
        css.append(f".l{n}{{animation:l{n} {cycle:.2f}s linear infinite}}"
                   f"@keyframes l{n}{{0%,{at:.2f}%{{opacity:0}}{at + 0.9:.2f}%,94%{{opacity:1}}100%{{opacity:0}}}}")
        color = "#FFB347" if text.startswith(("🏮", "📋", "💸")) else "#F2EBE1" if text.startswith(("📱", "💻")) else "#A79D92"
        if "✓" in text or "approved" in text:
            color = "#7EE2A8"
        if "needs your OK" in text:
            color = "#9DB8FF"
        rows.append(f'<text class="l{n}" x="34" y="{92 + n * lh}" fill="{color}">{escape(text)}</text>')
    cmd_at = 0.4 / cycle * 100
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="{h}" viewBox="0 0 1040 {h}" role="img"
  aria-label="The output of lantern --demo-run: a form filled and submitted after approval, an email sent and verified, a private file answered on this computer, and the audit.">
  <style>
    text {{ font-family: {MONO}; font-size: 15.5px; white-space: pre; }}
    .cmd {{ animation: cmd {cycle:.2f}s linear infinite; }}
    @keyframes cmd {{ 0%,{cmd_at:.2f}% {{opacity:0}} {cmd_at + 0.9:.2f}%,94% {{opacity:1}} 100% {{opacity:0}} }}
    {''.join(css)}
    @media (prefers-reduced-motion: reduce) {{ text {{ animation: none !important; opacity: 1 !important; }} }}
  </style>
  <rect width="1040" height="{h}" rx="16" fill="#171411"/>
  <rect x=".5" y=".5" width="1039" height="{h - 1}" rx="16" fill="none" stroke="#2C2722"/>
  <circle cx="26" cy="24" r="6.5" fill="#FF5F57"/><circle cx="48" cy="24" r="6.5" fill="#FEBC2E"/><circle cx="70" cy="24" r="6.5" fill="#28C840"/>
  <text x="520" y="29" text-anchor="middle" fill="#7D746A" style="font-size:13px">lantern --demo-run  ·  offline rehearsal, $0, no key</text>
  <text class="cmd" x="34" y="64" fill="#F2EBE1"><tspan fill="#FFB347">$</tspan> lantern --demo-run</text>
  {chr(10).join('  ' + r for r in rows)}
</svg>
"""


# ---- the FreeCAD demo's enclosure as an STL ---------------------------------------------------------------------------
def enclosure_stl(length=40.0, width=20.0, height=25.0, wall=2.0, hole_d=3.4, holes=((6.0, 10.0), (34.0, 10.0)),
                  segments=16) -> str:
    """demo/freecad/tasks.json: a 40 x 20 mm box, hollowed with 2 mm walls (open top), two M3 holes (3.4 mm) in the
    floor, then made 10 mm taller (25 mm). Plain triangles, for GitHub's 3D viewer."""
    tris: list[tuple] = []

    def quad(a, b, c, d):
        tris.extend([(a, b, c), (a, c, d)])

    L, W, H, t, r = length, width, height, wall, hole_d / 2
    # outer walls (no top) and inner walls
    quad((0, 0, 0), (L, 0, 0), (L, 0, H), (0, 0, H))
    quad((L, 0, 0), (L, W, 0), (L, W, H), (L, 0, H))
    quad((L, W, 0), (0, W, 0), (0, W, H), (L, W, H))
    quad((0, W, 0), (0, 0, 0), (0, 0, H), (0, W, H))
    quad((t, t, t), (t, t, H), (L - t, t, H), (L - t, t, t))
    quad((L - t, t, t), (L - t, t, H), (L - t, W - t, H), (L - t, W - t, t))
    quad((L - t, W - t, t), (L - t, W - t, H), (t, W - t, H), (t, W - t, t))
    quad((t, W - t, t), (t, W - t, H), (t, t, H), (t, t, t))
    # the rim
    quad((0, 0, H), (L, 0, H), (L - t, t, H), (t, t, H))
    quad((L, 0, H), (L, W, H), (L - t, W - t, H), (L - t, t, H))
    quad((L, W, H), (0, W, H), (t, W - t, H), (L - t, W - t, H))
    quad((0, W, H), (0, 0, H), (t, t, H), (t, W - t, H))

    def ring(cx, cy, z, half, up):
        """A square cell around a hole at height z: square edge to circle, one quad per segment."""
        for i in range(segments):
            pts = []
            for k in (i, i + 1):
                a = 2 * math.pi * k / segments
                c, s = math.cos(a), math.sin(a)
                m = half / max(abs(c), abs(s))
                pts.append(((cx + r * c, cy + r * s, z), (cx + m * c, cy + m * s, z)))
            (c0, s0), (c1, s1) = pts
            quad(c0, s0, s1, c1) if up else quad(c0, c1, s1, s0)

    def floor(x0, y0, x1, y1, z, up, half=3.5):
        (ax, ay), (bx, by) = holes
        ys = [y0, ay - half, ay + half, y1]
        rects = [(x0, ys[0], x1, ys[1]), (x0, ys[2], x1, ys[3]),
                 (x0, ys[1], ax - half, ys[2]), (ax + half, ys[1], bx - half, ys[2]), (bx + half, ys[1], x1, ys[2])]
        for (u0, v0, u1, v1) in rects:
            if up:
                quad((u0, v0, z), (u1, v0, z), (u1, v1, z), (u0, v1, z))
            else:
                quad((u0, v0, z), (u0, v1, z), (u1, v1, z), (u1, v0, z))
        for (cx, cy) in holes:
            ring(cx, cy, z, half, up)

    floor(0, 0, L, W, 0.0, up=False)
    floor(t, t, L - t, W - t, t, up=True)
    for (cx, cy) in holes:   # the holes' walls
        for i in range(segments):
            a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
            p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
            p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
            quad((*p0, 0.0), (*p0, t), (*p1, t), (*p1, 0.0))

    def num(v: float) -> str:
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    def normal(a, b, c):
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        m = math.sqrt(sum(x * x for x in n)) or 1.0
        return [x / m for x in n]

    out = ["solid lantern_enclosure"]
    for a, b, c in tris:
        n = normal(a, b, c)
        out.append(f"facet normal {' '.join(num(x) for x in n)}\nouter loop\n"
                   + "\n".join(f"vertex {' '.join(num(x) for x in p)}" for p in (a, b, c)) + "\nendloop\nendfacet")
    out.append("endsolid lantern_enclosure")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-run-log", required=True, help="the output of `lantern --demo-run`")
    a = ap.parse_args()
    MEDIA.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        (MEDIA / f"banner-{theme}.svg").write_text(banner(theme), encoding="utf-8")
    log = Path(a.demo_run_log).read_text(encoding="utf-8")
    (MEDIA / "demo-run.svg").write_text(terminal(terminal_lines(log)), encoding="utf-8")
    stl = enclosure_stl()
    (MEDIA / "enclosure.stl").write_text(stl, encoding="utf-8")
    print(f"banner-light.svg, banner-dark.svg, demo-run.svg, enclosure.stl ({stl.count('facet normal')} triangles, "
          f"{len(stl) // 1024} KB) in {MEDIA}")
