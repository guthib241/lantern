# Lantern's face (made with Muse)

Fill this folder in one of two ways:

- **Muse Image API:** run `python scripts/make_avatar.py`. It renders a canonical character, then each expression anchored on it.
- **MetaMuse app / Vibes:** export your art and run `python scripts/import_avatar.py <folder>`, with files named `idle.png`, `thinking.mp4` and so on.

Expressions: idle, thinking, smug, annoyed, celebrating, needs-approval, sleeping. `manifest.js` tells the desktop app which file shows each expression; missing ones show idle.
