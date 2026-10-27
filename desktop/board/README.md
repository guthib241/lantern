# Lantern teaching board (S16)

A standalone web page that plays a lesson: a JSON timeline of board actions on a chalkboard, each with a line that is spoken aloud and shown as a caption. It has no build step and no network use. It can be dropped into the Electron app later as another window.

## Try it

```bash
cd desktop/board && python -m http.server 8780
# open http://127.0.0.1:8780/  (or ?lesson=derivative, ?speed=1.5, ?mute=1, ?autoplay=1)
```

**Controls:** **Play/Pause** (Space), **Step** (→, one action then pause), **Restart** (R), **Voice on/off** (M; captions stay), speed, the lesson picker, and **Open lesson file…**. When the page is opened straight from disk, `file://` can't fetch the lessons, so use the file picker.

## The voice

Spoken lines use the browser's or operating system's own speech engine (the Web Speech API: Windows SAPI voices in Electron on Windows). **It is not a Meta model**, and the page says so under the captions. If there is no voice, captions carry the lesson and timing falls back to reading speed.

## Lessons

A lesson is JSON; [`schema/lesson.schema.json`](schema/lesson.schema.json) is the contract (JSON Schema 2020-12). The board is 1600 × 900 units, and `x`, `y` are top-left positions. Optional fields: `say` (on every action), `id` (to target the item later), `color` (`chalk`, `yellow`, `blue`, `orange`, `green`, `pink`, `red` or `#rrggbb`) and `seconds` (animation length).

| Action | Fields | What happens |
|---|---|---|
| `write_math` | `latex`, `x`, `y`, `size`, `step`, `verified`, `check` | KaTeX, revealed left to right; a ✓ appears in its slot if `verified` is true; `step` names the transformation |
| `write_text` | `text`, `x`, `y`, `size` | handwriting-style text, revealed left to right |
| `draw` | `shape`: `line`/`arrow` (`from`, `to`), `circle` (`center` + `r`, optionally on `axes`; or `around` an item), `axes` (`x`, `y`, `w`, `h`, `xrange`, `yrange`, `ticks`, labels), `plot` (`axes`, `points`, `label`, `label_at`) | strokes drawn like chalk |
| `highlight` | `target` | a marker stroke behind an item |
| `erase` | `target`: an id, a list of ids, or `"all"` | fades out and removes |
| `pause` | `seconds` | waits |

### The ✓

The board never does math. A ✓ shows only when a line says `"verified": true`. The lesson planner sets that after SymPy accepts the step (PHASE2 S16). The line's `check` keeps the step in SymPy syntax with its relation to an earlier line (`given`, `same_solutions`, `equal`, `derivative_of`, `derivatives`), so anyone can re-verify it. `tests/parallel/test_parallel_board.py` re-verifies every ✓ in the sample lessons with SymPy. `lib/timeline.js` also refuses a lesson whose `verified` line has no `check`.

## Sample lessons

- `lessons/quadratic.json`: solve x² − 5x + 6 = 0 by factoring (4 checked lines), then circle the roots on the parabola's graph.
- `lessons/derivative.json`: differentiate x²·sin x with the product rule (4 checked lines; the helper lines are erased), then plot f and f′ and point at a flat spot.

## Files

- `index.html`, `board.css`, `board.js`: the page. `window.LanternBoard` exposes `load`, `play`, `pause`, `step`, `restart` and `state` for the desktop app and tests.
- `lib/timeline.js`: the pure parts (validation of ids and references, timing, axes coordinates). It runs in the page and under node.
- `vendor/katex/`: KaTeX 0.19.0 (MIT): minified JS and CSS plus the woff2 fonts only.
