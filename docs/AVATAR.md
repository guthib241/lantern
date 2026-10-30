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

## Cost

Each image costs $0.01. The preview is one image; finishing a face is six
more. Every image is logged to memory like any other model call, so the
Costs tab shows what the face cost.
