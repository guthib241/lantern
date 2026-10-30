/* Where the avatar face comes from. Pure.
 * The server's active design first, then the art shipped in the repo
 * (desktop/assets/avatar/manifest.js), then the built-in glow. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternAvatar = api;
})(typeof self !== 'undefined' ? self : this, function () {
  /** Server manifest ({active, expressions}) and bundled one ({expressions}) -> where to load. */
  function pickSource(server, bundled) {
    if (server && server.active && server.expressions && Object.keys(server.expressions).length) return 'server';
    if (bundled && bundled.expressions && Object.keys(bundled.expressions).length) return 'bundled';
    return 'glow';
  }

  return { pickSource };
});
