# FreeCAD on the Windows laptop (S17)

First real-hardware run of the FreeCAD connector, Oct 30, 2026. Branch `claude/freecad-laptop` (= `claude/freecad-connector` + merge hooks + Windows fixes).

## The laptop
- Windows 11 (10.0.26300), Command Prompt (not PowerShell)
- FreeCAD **1.1.0** (Revision 20260325), installed per-user: `%LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecadcmd.exe`
- Python 3.12.10 (`py -3.12`; the default `python` is 3.10, too old), git 2.56, Node.js 24

## Exact steps that worked (Command Prompt)
```cmd
cd %USERPROFILE%
git clone https://github.com/guthib241/lantern.git
cd lantern
git checkout claude/freecad-laptop
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt pytest
python -m pytest -q tests\freecad

rem API key: one line MODEL_API_KEY=... in the repo's .env (gitignored; check with: git check-ignore .env)
notepad .env

python -c "from lantern import config; from lantern.integrations.freecad.connector import add_to_mcp_json; print(add_to_mcp_json(config.load(), freecad_cmd=r'%LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecadcmd.exe'))"
mkdir "%USERPROFILE%\Documents\CAD"
python -m lantern --set-zone "%USERPROFILE%\Documents\CAD" writable

python demo\freecad\run_demo.py --freecad
python demo\freecad\run_demo.py --freecad --spark

cd desktop
npm install --fetch-retries=5 --fetch-retry-maxtimeout=120000
npm start
```

Notes:
- A Windows *user environment variable* for `MODEL_API_KEY` did not reach the open Command Prompt; the `.env` file is simpler and is what Lantern reads anyway.
- The first `npm install` failed with `ECONNRESET` (network); retrying with `--fetch-retries` worked.
- The desktop avatar is a glowing orb that floats above every window, also after the laptop wakes up. A first-time user mistook it for screen damage. Consider a short "Lantern: click me" label on first start.

## Results
| Check | Result | Time | Meta cost |
|---|---|---|---|
| `pytest tests\freecad` (incl. the real-FreeCAD test) | 65 passed, 0 skipped | 37 s | $0 |
| `run_demo.py --freecad` (3 tasks, real FreeCAD) | 3/3 pass: 40×20×15 enclosure, 2 × Ø3.4 holes, 2 mm walls; phone stand 80 mm at 60.0°; +10 mm and STL export (52,284 bytes, verified) | not timed | $0 |
| `run_demo.py --freecad --spark` | 3/3 pass; Muse Spark said yes to all 5 visual checks on every part | 61 s | ≈$0.0075 (3 Spark calls; Lantern's cost meter) |
| Lantern desktop, request 1: "Make an enclosure 40×20×15 mm with 2 mm walls and two M3 mounting holes in the floor." | Built through Lantern's real Core with 6 desktop approvals (new_document, box, hollow, hole, set_dimension, hole) and 7 read-only checks | not timed | $0.09 (16 Muse Spark calls, 255 KB sent) |

**Not confirmed yet (to do):**
- request 1's final reply and check verdict (the `set_dimension` between the holes looks like the one allowed fix);
- request 2, the red export card and the STL in `Documents\CAD`;
- request 3, measuring with no approval;
- opening the exported STL in FreeCAD;
- CAD from the phone.

Independent check of an exported STL with FreeCAD itself (free):
```cmd
"%LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecadcmd.exe" -c "import Mesh; m=Mesh.Mesh(r'%USERPROFILE%\Documents\CAD\enclosure.stl'); b=m.BoundBox; print(round(b.XLength,2), round(b.YLength,2), round(b.ZLength,2), round(m.Volume,1), m.isSolid())"
```

## Windows bugs found and fixed
1. **FreeCAD per-user installs and newer versions weren't found.** It is now also searched in `FreeCAD*` folders under Program Files and `%LOCALAPPDATA%\Programs`, newest first.
2. **Restart after a FreeCAD crash failed with `[Errno 22] Invalid argument`.** Windows reports a write to a just-exited worker as EINVAL before the process is reaped. The backend now reaps on EOF and restarts once.
3. **The demo reported a written, verified STL as missing.** It compared paths as text (`real/enclosure.stl` vs `real\enclosure.stl`); it now compares them as paths.
4. **The demo's `--spark` mode ignored `.env`.** It now loads it the way Lantern does.
5. **The desktop app started the server with `py`.** On Windows that is the system Python without Lantern's packages; the app now uses the checkout's `.venv` Python when it exists.

## Spend so far
About **$0.10** of Meta credit for all laptop testing (Lantern's own "Spent today" meter).
