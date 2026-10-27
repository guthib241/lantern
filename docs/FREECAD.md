# S17 — FreeCAD connector

Lantern does real CAD work in FreeCAD from plain English:
- create parts from primitives (box, cylinder, a sketched profile extruded);
- holes, shells, fillets and chamfers;
- booleans;
- edit a dimension of an existing part;
- measure (size, volume, distances, intersections);
- export STL / STEP / DXF for 3D printing or CAD exchange.

After every change it measures the part with FreeCAD's own numbers and renders a picture. **Muse Spark** looks at the picture together with your goal and a structured checklist. Lantern never says "done" unless those checks pass.

Branch: `claude/freecad-connector` (new files only; the hooks into existing files are listed under [Merge hooks](#merge-hooks)).

## 1. Which MCP server, and why a new one

Searched GitHub on Oct 27, 2026 and read each project's license and README. Repositories were cloned read-only; no code was copied.

| Project | License | Last commit | How it works | Verdict |
|---|---|---|---|---|
| [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) | MIT | 2026-10-25 | FreeCAD GUI add-on + XML-RPC; tools such as `create_object`, `edit_object`, `delete_object`, `get_view`, and **`execute_code`** | The most used, but most real work goes through `execute_code` (arbitrary Python inside FreeCAD) and it needs the GUI running |
| [spkane/freecad-addon-robust-mcp-server](https://github.com/spkane/freecad-addon-robust-mcp-server) | MIT | 2026-10-27 | Add-on + Docker/PyPI; includes **`execute_python`** | Same issue: arbitrary code |
| [bonninr/freecad_mcp](https://github.com/bonninr/freecad_mcp) | MIT | 2025-03-20 | GUI add-on, socket bridge | Inactive for 19 months |
| [theosib/FreeCAD-MCP-Server](https://github.com/theosib/FreeCAD-MCP-Server) | none | 2026-03-04 | Debugging-oriented bridge | No license: can't be used |
| [lucygoodchild/freecad-mcp-server](https://github.com/lucygoodchild/freecad-mcp-server) | none | 2025-06-02 | Node.js, basic shapes + custom scripts | No license; small |

**Decision: write a small new MCP server** (`lantern/integrations/freecad/`, new code). Lantern's security rules are enforced in code (CLAUDE.md):
- every file write must be checked against privacy zones;
- every export must show the exact file in a red approval;
- nothing may run arbitrary code.

A server whose real power is `execute_code` / `execute_python` would bypass all three: the model could write any file, or read private folders, through FreeCAD. The existing servers also need the FreeCAD GUI with an add-on loaded. Ours drives FreeCAD headless (`FreeCADCmd`), which the verify loop and the demo need anyway. neka-nat's server remains a good alternative for people who want a live GUI session. It can be added to `mcp.json` as a separate server, but it should not get Lantern's writable zones, and its `execute_code` tool must stay behind approvals.

**Licenses that apply to us:**
- FreeCAD is LGPL-2.1-or-later. Lantern doesn't link to it or ship it. It starts the user's installed `FreeCADCmd` as a separate process and talks to a worker script over stdin/stdout. The LGPL allows that, and nothing about Lantern's own license changes.
- None of the candidates was GPL. Had one been, launching it as a separate process over stdio would generally count as aggregation rather than a derived work. We would still have to pass on its license and source if we redistributed it, and we'd keep it as an optional, separately installed server.
- No new Python packages: the MCP SDK (already used by Lantern) and the standard library.

## 2. How it fits together

```
Muse Spark (agent) --tool_search--> freecad.* deferred tools
        |                                   |
   Lantern Toolbox --(hook)--> FreeCADConnector: tiers, approvals, verify loop
        |                                   |
   Lantern MCP client (mcp_client.MCPHub) --stdio--> server.py (MCP server, Lantern's Python)
                                                       |  typed ops (ops.py), zone checks, file writes via policy
                                                       v
                                       backend.py -> worker.py inside FreeCADCmd (FreeCAD's Python)
                                                       |  parametric feature tree (tree.py) evaluated with Part
                                       (or fake.py: the same tree on voxel geometry, for tests and the cloud)
```

- **`ops.py`**: the only operations: `new_document`, `box`, `cylinder`, `sketch_extrude`, `hole`, `hollow`, `fillet`, `chamfer`, `boolean`, `set_dimension`, `move`, `rotate`, `measure`, `distance`, `list_objects`, `render`, `save`, `export`. Every argument is typed and bounded: names are identifiers, sizes are 0–2000 mm, formats come from a fixed list. **There is no "run code" operation** (a test checks this).
- **`tree.py`**: the document is a parametric feature tree.
  - A hole or a shell keeps the old feature as its base.
  - `set_dimension` changes the primitive deep in the tree, and holes and shells follow, just as "make it 10 mm taller" should behave.
  - The same tree is used by the fake and by the FreeCAD worker, so tests and laptop behave alike.
- **`worker.py`**: runs inside FreeCADCmd and builds the tree with FreeCAD's `Part` kernel:
  - building: `makeBox`, `makeCylinder`, `Face.extrude`, `cut`, `fuse` / `removeSplitter`, `common`, `makeThickness`, `makeFillet`, `makeChamfer`;
  - measuring: `BoundBox`, `Volume`, `isValid`, `distToShape`, `tessellate`;
  - exporting: `Mesh` for STL, `exportStep`, and TechDraw `projectEx` for the DXF top view.

  It only ever writes to a temporary path the server gives it.
- **`server.py`**: the MCP server.
  - After every change it returns fresh measurements, the hole/shell/profile features, and a PNG render.
  - Saves and exports: checks the zone and extension → FreeCAD writes a temp file → `lantern.policy.write_bytes` writes the real file → the file is read back to confirm it (a real STL / STEP / DXF).
  - An existing file is only replaced with `overwrite=true`.
- **`render.py`**: a small z-buffer renderer (standard library only) that draws isometric, front and top views with a 10 mm scale bar, for the image model.

## 3. Risk tiers (S0.3), enforced in code

| Tier | Tools | What happens |
|---|---|---|
| green | `measure`, `distance`, `list_objects`, `render_view`, `check_model` | Runs automatically (listed as `read_only` in mcp.json) |
| yellow | `new_document`, `box`, `cylinder`, `sketch_extrude`, `hole`, `hollow`, `fillet`, `chamfer`, `boolean`, `set_dimension`, `move`, `rotate`, `save` to a **new** file | One-tap OK, e.g. "Drill a Ø3.4 mm hole (through) in 'enclosure' at (6, 10)" |
| red | every `export` (STL, STEP, DXF), and a `save` that **replaces** a file | Full approval naming the **exact file**, the format and "REPLACES the existing file" when it does. It also shows the last check result ("Checks: passed ✓" or "NOT passed ✗ (…)"). Bound to the payload hash. |

- **Files:** only in writable zones, with the right extension. Hidden zones, private zones, `..` escapes and symlinks out are refused before any approval card or FreeCAD call. The server checks again itself, so a path outside the zones is refused even before the merge hook is in.
- **Declining** an approval writes nothing.

## 4. The visual verify loop

1. **Expectations:** Muse Spark turns the goal into a typed checklist (structured output): size per axis, hole count and diameter, wall, angle, number of solids. `check_model` arguments, or a demo task, can provide them directly.
2. **Numbers from FreeCAD**, which decide pass/fail:
   - bounding box per axis (±0.2 mm);
   - hole count, counted as **openings**: one cut through two walls = 2, measured on a line beside the hole's axis;
   - hole diameters, and holes inside the part outline;
   - wall thickness, and the sketch profile's angles;
   - `isValid` and solid count;
   - optional volume range;
   - pairs of parts not intersecting (`distance`);
   - exported file written and verified.
3. **Eyes:** the 3-view PNG goes to Muse Spark (image understanding) with the goal, the measured numbers and a fixed checklist: `dimensions_match`, `holes_as_asked`, `no_intersections`, `valid_single_solid`, `matches_goal`. The answer is strict JSON with yes / no / unsure / not_applicable per item.
4. **Verdict:**
   - It passes only when every numeric check passes and no visual check says "no".
   - "unsure" never turns a failure into a pass.
   - If Spark is unreachable, the numbers decide and the verdict says "no visual check".
5. **One fix attempt:** after a failed `check_model` the agent may change the model once and check again. A second failure returns `give_up` with "Do not say it is done": the agent must report honestly what is still wrong.
6. **Cost:** every Spark call is logged to the task (receipts and spending limits see it).

`visual="every_change"` runs the eyes after each change (the default, as the spec asks). `"on_check"` runs them only at `check_model`, which is cheaper. `"off"` uses numbers only.

## 5. Setup on Windows

1. **Install FreeCAD 1.0 or newer** from freecad.org (Windows installer). It comes with its own Python, so nothing else is needed for FreeCAD.
2. **Find `freecadcmd.exe`**: usually `C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe` (older versions: `FreeCADCmd.exe`). Check it in PowerShell:
   ```powershell
   & "C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe" --version
   ```
3. **Register the server in Lantern's MCP config.** Either let Lantern write the entry:
   ```powershell
   python -c "from lantern import config; from lantern.integrations.freecad.connector import add_to_mcp_json; print(add_to_mcp_json(config.load(), freecad_cmd=r'C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe'))"
   ```
   or copy `config/mcp/freecad.example.json` into `%USERPROFILE%\.lantern\mcp.json` and fix the paths. The entry holds no secrets.
4. **Make an output folder writable**, e.g. `Documents\CAD`. Privacy zones decide where Lantern may write:
   ```powershell
   python -m lantern --set-zone "$HOME\Documents\CAD" writable
   ```
5. **Rehearse without FreeCAD, then with it:**
   ```powershell
   python demo\freecad\run_demo.py              # fake geometry, numbers only (free)
   python demo\freecad\run_demo.py --spark      # + Muse Spark visual checks (a few cents)
   python demo\freecad\run_demo.py --freecad    # real FreeCAD
   python -m pytest -q tests\freecad            # the real-FreeCAD test runs when freecadcmd is found
   ```
6. **Start Lantern** as usual (`cd desktop && npm start`, or `python -m lantern --serve`). The FreeCAD tools appear as deferred tools: Muse Spark finds them with tool search when a request is about CAD.

**Troubleshooting:**
- "FreeCAD isn't installed": set `LANTERN_FREECAD_CMD` in the mcp.json `env`.
- "outside the folders I'm allowed to touch": that path isn't in a writable zone.
- "already exists; … overwrite=true": approve the red card that replaces it.
- A slow first call: FreeCAD takes a few seconds to start; the worker then stays running.

## 6. What to try
- "Make an enclosure 40×20×15 mm with 2 mm walls and two M3 mounting holes in the floor."
- "Make a phone stand that holds the phone at 60 degrees, 80 mm wide, with a small front lip."
- "Make the enclosure 10 mm taller and export it as an STL for 3D printing." (Red card with the file path.)
- "Put a 5 mm cable hole through the middle of each short side wall." (Cylinder + boolean cut; counted as 2 openings.)
- "Round the vertical edges by 2 mm and export STEP and a DXF of the outline."
- "How big is it, what's the volume, and do the two parts touch?" (green: no approval needed)

The scripted versions of the first three, with their expected checks, are in `demo/freecad/tasks.json`.

## 7. What has been verified, and what hasn't

Verified on Oct 27 in the cloud sandbox:
- **Live Muse Spark:** the 3 demo tasks pass the numbers and the visual check.
  - Spark turned "enclosure 40x20x15 mm, 2 mm walls, two M3 mounting holes" into size 40/20/15, wall 2, 2 holes of Ø3.4 mm (correct M3 clearance), 1 solid.
  - **Visual-only catch:** asked for holes in the side walls, given holes in the floor, Spark answered `holes_as_asked = no`, so the check failed even though the numbers passed.
  - Fix-then-pass and fix-then-give-up both behaved as specified.
  - Full enclosure task: about $0.03 with checks after every change.
- **Live, plain English through Lantern's real `Core`** (the documented hooks applied in the test script). Core started the server from mcp.json as a separate process; Muse Spark found the 19 tools through tool search.
  - "make it 10 mm taller and export it as an STL into my CAD folder": done in 111 s for $0.10. Set height → check passed → red card naming the exact file → STL written and verified.
  - "phone stand at 60 degrees, 80 mm wide, with a small front lip": done in 95 s for $0.08. The first attempt failed the after-change check, Spark started over, and the second passed.
  - The enclosure request also built correctly, but once Meta's API answered "referenced response not found or expired" mid-task. That is an intermittent server fault (also seen once outside CAD; not reproducible on purpose). The fix, continuing the task from a recap of its steps, is in core `agent.py` on branch `claude/live-api-qa`. Merge both.
  - Spark once drilled two different hole sizes (Ø3 and Ø3.4) for the same M3 screws. The final check's hole-diameter test exists for exactly that.
- **Live connector-level runs:** fixed two bugs found this way.
  - Muse Spark asked for format "STL" and the task crashed with a KeyError. Formats are now case-insensitive, and any unexpected error comes back to the model as a tool error instead of failing the task.
  - Restarting a crashed or hung FreeCAD worker read a stale end-of-stream marker. Each worker process now gets its own reply queue.
- **59 tests in `tests/freecad`** (58 passed, 1 skipped), all offline with `FakeClient`:
  - operation rules, tree and geometry, renders;
  - tiers and zones, approvals, declined exports;
  - the server through Lantern's real MCP client;
  - the verify loop's pass / fail / unsure / give-up paths;
  - the FreeCAD process protocol (chatter, errors, hang, crash and restart);
  - **the real `worker.py` against stub FreeCAD modules**;
  - the demo end to end;
  - the merge hook itself, applied with monkeypatch: a full agent task builds, checks and exports, and is told to stop and be honest after a failed fix.

**Not verified yet:** the worker against **real FreeCAD**. FreeCAD couldn't be installed in the cloud sandbox: Ubuntu 24.04's packages don't include it, and downloading installers from outside is blocked there. The worker uses standard, long-stable `Part` / `Mesh` / `TechDraw` APIs, but expect to fix a detail or two on the first laptop run. **First laptop step:** `python demo\freecad\run_demo.py --freecad`. If something fails, the error says which FreeCAD call broke (`FreeCAD: <exception>`).

**Known limits:**
- The `hole` tool drills vertically from the top. Side holes are made with `cylinder` (axis x or y) plus `boolean cut`, which the checks understand.
- Fillet and chamfer pick edges by group (all / vertical / top / bottom), not one by one.
- The fake backend's volume is approximate (about ±6%; marked `approx`), and its STEP export is a labelled placeholder. Real FreeCAD gives exact values and real files.
- DXF export is the top view's visible edges.

## Merge hooks

These touch existing files, so they were **not** made on this branch. Add them when merging:

1. **`lantern/tools.py`, `Toolbox.mcp_call`**: route the freecad server through the connector (tiers, one-tap yellow, red file lists, verify loop). First line of the method body:
   ```python
   if getattr(self, "freecad", None) is not None and self.mcp.tools[function].server == "freecad":
       return self.freecad.call(self, function, args)
   ```
   and in `Toolbox.__init__`: `self.freecad = None`.
2. **`lantern/core.py`, after the Toolbox is built**: give it the connector, with Lantern's guarded client, so Spark calls are scrubbed, audited and budgeted:
   ```python
   if "freecad" in getattr(self.mcp, "servers", {}):
       from .integrations.freecad.connector import FreeCADConnector
       self.toolbox.freecad = FreeCADConnector(self.policy, self.client, self.memory,
                                               on_event=lambda k, d: self.emit({"type": k, **d}))
   ```
3. **`lantern/agent.py`, `BASE_RULES`** (optional, one line; keeps the prefix byte-stable once added): `- For CAD requests, find the freecad tools with tool search; call freecad.check_model before saying a part is done, and never claim success when it fails.`
4. **`docs/THIRD_PARTY.md`**: add the row `| FreeCAD (user-installed, run as a separate process) | LGPL-2.1-or-later | CAD kernel for S17 (not bundled) |`.
5. **`lantern/doctor.py`** (optional): a check that `find_freecadcmd()` finds FreeCAD when mcp.json has a `freecad` server, with the fix "install FreeCAD 1.0+ or set LANTERN_FREECAD_CMD".
6. **`docs/DECISIONS.md` and `BUILD_LOG.md`**: paste the two entries below.

**DECISIONS.md entry:** Oct 27 (S17): FreeCAD connector is a new, small MCP server with typed operations only. The popular open-source servers (MIT) do their real work through `execute_code` / `execute_python`, which would bypass privacy zones and red approvals, and they need the FreeCAD GUI. FreeCAD runs as a separate `FreeCADCmd` process (LGPL, not bundled) with a JSON-lines worker; the parametric feature tree is shared with a voxel fake, so tests and the cloud behave like the laptop. Exports are always red with the exact file; files are written through `lantern.policy`. The verify loop is numbers from FreeCAD plus Muse Spark looking at a 3-view render (structured output), one fix attempt, then an honest report.

**BUILD_LOG.md entry:** S17 FreeCAD connector (`lantern/integrations/freecad/`, `demo/freecad/`, `tests/freecad/`, `docs/FREECAD.md`, `config/mcp/freecad.example.json`):
- typed CAD tools as deferred MCP tools behind tool_search;
- green/yellow/red tiers with zone checks and exact-file red approvals;
- the visual verify loop on Muse Spark;
- three demo tasks;
- 59 offline tests.

Verified live with Muse Spark on the fake geometry, including plain-English tasks through Core. Real FreeCAD is laptop-only.
