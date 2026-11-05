# T3e: Signed agent packs

**Goal:** a shared agent pack shows **who made it** and whether it was **changed** since they made it. The panel shows "✓ Signed by achan · key 3F2A-91C0", "Unsigned", or **refuses** a pack whose signature doesn't match (tampered). Do this after T3a/T3b.

Read `CLAUDE.md`. Write the code yourself; do not copy code from other projects. Use the `cryptography` package (already a dependency) for Ed25519. `python -m pytest -q` must pass before committing.

## Build
1. **`lantern/packsign.py`** (new):
   - `ensure_key(home) -> (private_key, public_key_b64)`: one Ed25519 key per user in `~/.lantern/keys/pack-signing.key`.
     - The file is readable only by its owner (`chmod 600` where supported). Encrypt it with Lantern's existing at-rest cipher if `lantern/crypto.py` offers one; check first.
     - The private key never leaves the laptop and is never logged.
   - `fingerprint(public_key_b64) -> str`: the first 8 hex characters of SHA-256, shown as `3F2A-91C0`.
   - `sign(pack_dict, private_key) -> dict`: adds `"signature": {"alg": "ed25519", "public_key": "<b64>", "sig": "<b64>"}`. The signature covers `agentpacks.canonical_bytes` of the pack **without** the `signature` field.
   - `verify(pack_dict) -> "signed" | "unsigned" | "tampered"`.
2. **`agentpacks.validate`:**
   - allow the optional `signature` key (strict shape, base64 checked);
   - a `tampered` pack is **refused** with "this pack was changed after it was signed".
3. **Trust on first use:** `~/.lantern/agents/trusted_keys.json` maps a fingerprint to an author name.
   - **Installing a signed pack from a new key:** the install approval says "New author key 3F2A-91C0 (achan). Trust it?". Approving records it.
   - **The same author name with a different key:** a **red** warning in the approval ("achan's packs were signed with a different key before").
   - **Unsigned packs** still install, but their card says "Unsigned: anyone could have changed it".
4. **Signing your own packs:** `POST /agents/{name}/sign` signs with your key, after an approval. Export offers "Sign before sharing" when the pack's author is you (`LANTERN_AUTHOR` or the profile name).
5. **Re-verify on every run,** not only at install. A file edited on disk afterwards must be caught.
6. **The Agents tab** (`desktop/renderer/panel.*`): a signature badge on each card, and the fingerprint in the install approval.

## Tests (`tests/test_packsign.py`)
- sign then verify gives `signed`;
- one changed character in instructions gives `tampered` and install refuses it;
- no signature gives `unsigned`;
- a forged signature (another key, same author) is flagged;
- trust-on-first-use records the key, and the second install of the same key doesn't ask "trust?" again;
- the key file is created once;
- the private key never appears in exported packs or logs (grep the export bytes);
- a run re-verifies (edit the stored file → the run is refused).

**Commit:** "T3e signed agent packs (Ed25519, trust on first use, re-verified on every run)".
