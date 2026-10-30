# Lantern's avatar: the studio

Lantern's face is drawn by **Muse Image**, never by hand. You describe it in
words in the panel (Setup → Avatar), pick a style, and Lantern draws it.

## How a design works

1. **Preview.** You write a short brief (at most 600 characters — a paragraph,
   not an essay) and pick one of four styles: 3d, flat, pixel or neon. Lantern
   scrubs secrets out of the brief, then draws **one** canonical portrait
   (about $0.01) and shows it to you. Nothing is saved on screen yet.
2. **Use this.** If you like the preview, one tap draws the other six
   expressions — thinking, smug, annoyed, celebrating, needs-approval,
   sleeping — each anchored on the preview so the character stays the same.
   This is the part that costs money (7 images, about $0.07), so it asks for
   your approval first, showing the estimated cost. Saying no leaves the
   preview untouched and puts nothing on screen.
3. **Gallery.** Every design is kept, so you can switch back to an older face
   for free: finished designs need no new drawing and no approval.

Designs live in Lantern's own folder (`~/.lantern/avatar/<id>/`), never in
the repo. The `active` file there names the design on screen.

## What the screen shows

The avatar window asks the server for the active design first. If there is
none yet, it shows the art shipped with the app
(`desktop/assets/avatar/manifest.js`, made with `scripts/make_avatar.py` or
imported from the MetaMuse app). If there is no art at all, it shows the
built-in glow. Every image prompt says "no logos, no text, no brand marks",
so the face is always ours, never Meta's branding.

## Live skins

The avatar window can also wear a **skin** instead of the drawn face. Pick
it in the panel under Setup → Avatar → Skin; it is saved with the other
preferences (`POST /settings`) and costs nothing — no drawing involved.

There are three skins: **Face** (the Muse portraits above, falling back to
the glow when there is no art yet), **Glow** (always the built-in glow), and
**Clock**. The clock is an analog clock drawn as our own SVG — like the glow,
with no text inside — showing the real local time on your computer, redrawn
every second. Lantern's state tints it: while working the ring pulses, while
it waits for your approval the ring turns amber, and when stopped it goes
grey.

Skins live in one small file (`desktop/renderer/lib/skins.js`), one function
per skin, so new ones can be added without touching the avatar window itself.

## Agent faces

When agent packs run, Lantern visibly turns into its agents: while pack tasks
run, the avatar window shows one small face per running agent beside the main
avatar, each with a green activity dot. Clicking a face opens that task's
timeline in the panel; right-clicking stops everything (the existing kill
switch — there is no per-task stop yet). When the last one finishes, the faces
fold back into the main avatar. Faces sit in a plain row, so they need no
`prefers-reduced-motion` exception: the row never moves, and the global
reduced-motion rule stills the dot.

A face is either an emoji or one drawn icon. Give an agent one in the Agents
tab with **Make a face**: type an emoji and save it (free, kept at once), or
have Lantern draw an icon (one Muse Image picture, about $0.01, behind an
approval showing the cost). The icon is stored next to the pack as
`~/.lantern/agents/<name>.webp`; the emoji lives in the laptop's pack wrapper
next to it. Sharing a pack (`GET /agents/{name}/export`) sends only the
data-only pack file, never the face — hand over the `.webp` yourself if you
want someone else to have the icon too.

Every drawn icon prompt carries "no logos, no text, no brand marks", like the
avatar prompts, and its cost is logged to memory like any other model call.

## Cost

Each image costs $0.01. The preview is one image; finishing a face is six
more. Every image is logged to memory like any other model call, so the
Costs tab shows what the face cost.
