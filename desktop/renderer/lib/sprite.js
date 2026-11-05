/* Stand-in shown only until Lantern's Muse-made avatar exists (desktop/assets/avatar/, from
 * scripts/make_avatar.py or images imported from the MetaMuse app). Deliberately not a character:
 * a faceless glow whose colour follows the state. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternSprite = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const COLORS = {
    idle: ['#fff1c9', '#ffb347'],
    thinking: ['#fff1c9', '#f59e0b'],
    smug: ['#fff1c9', '#ffb347'],
    annoyed: ['#fde2e2', '#ef4444'],
    celebrating: ['#e8fbe9', '#22c55e'],
    'needs-approval': ['#ece6ff', '#6d4aff'],
    sleeping: ['#efe9f5', '#9a8fb0'],
  };

  function svg(expression) {
    const [inner, outer] = COLORS[expression] || COLORS.idle;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 240" role="img"
  aria-label="Lantern is ${expression} (Muse avatar not generated yet)">
  <defs>
    <radialGradient id="g" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="${inner}"/><stop offset="70%" stop-color="${outer}" stop-opacity="0.85"/>
      <stop offset="100%" stop-color="${outer}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <circle class="orb" cx="100" cy="130" r="92" fill="url(#g)"/>
</svg>`;
  }

  return { svg, COLORS };
});
