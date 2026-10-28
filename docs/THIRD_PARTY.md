# Third-party libraries

| Library | License | Use |
|---|---|---|
| openai (Python SDK) | Apache-2.0 | HTTP client for Meta Model API |
| python-dotenv | BSD-3 | load .env |
| pytest | MIT | tests |
| FastAPI | MIT | local API server on 127.0.0.1 |
| Uvicorn | BSD-3 | ASGI server for FastAPI |
| HTTPX | BSD-3 | FastAPI test client (tests only) |
| Playwright (Python) | Apache-2.0 | Lantern's own browser for form filling (M7) |
| ffmpeg (external binary, not bundled) | LGPL-2.1+/GPL | converts voice notes to 16 kHz WAV (M1, M8) |
| websockets | BSD-3 | WebSocket support for Uvicorn (the desktop app's live events) |
| Electron 44.5.1 (desktop, dev dependency) | MIT | the avatar overlay and panel windows |
| mcp (Model Context Protocol Python SDK 2.x) | MIT | MCP client (other tools as deferred tools) and Lantern's MCP server |
| cryptography (Fernet) | Apache-2.0 / BSD-3 | encrypting memory and the audit log at rest |
| keyring | MIT | keeping the encryption key in the OS keychain |
| meta_sam_parser (Meta) | SAM License | decoding SAM 3.1 segmentation streams (camera check-in, photo to scan) |
| cedarpy (optional extra `network`) | Apache-2.0 | Lantern Network (S6): checks every request from another Lantern against the owner's Cedar policy |
| Pillow | MIT-CMU (HPND) | photo to scan (S10): open the photo, straighten the page, save PNG and PDF; Check my work (S16): circle the wrong line |
| KaTeX 0.19.0 (vendored in `desktop/board/vendor/katex`) | MIT | renders the teaching board's math (S16) |
| SymPy (optional extra `teach`) | BSD-3-Clause | Teaching Mode (S16): checks every math step of a lesson, and each line of handwritten working, before a check mark is shown |
| pypdf (optional extra `cv`) | BSD-3-Clause | S15: reads the text of the user's own PDF resume for the forms profile |
| setuptools (build only) | MIT | builds the `lantern` package and command from `pyproject.toml` |
| pyflakes (dev only) | MIT | lint in CI: unused imports, undefined names |
| GitHub Actions: actions/checkout, actions/setup-python, actions/setup-node (CI only) | MIT | runs the tests, the rehearsal and the doctor on each pull request |
| axe-core 4.13.0 (desktop, dev dependency) | MPL-2.0 | accessibility checks (WCAG 2.1 AA) in the end-to-end test; never shipped in the app |
