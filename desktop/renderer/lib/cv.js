/* Resume → profile (S15) in the panel: which proposed values start ticked, and what Save sends. Pure. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternCV = api;
})(typeof self !== 'undefined' ? self : this, function () {
  /** One row per proposed value; ticked when the profile has nothing there yet, or something different. */
  function rows(proposal) {
    return ((proposal && proposal.fields) || []).map((f) => {
      const current = String(f.current || '');
      const value = String(f.value || '');
      return { key: String(f.key), value, current, checked: current !== value,
               note: !current ? 'new' : current === value ? 'already saved' : `replaces “${current}”` };
    });
  }

  /** The POST /profile bodies for the ticked rows. */
  function saves(list, ticked) {
    return list.filter((r) => ticked.has(r.key)).map((r) => ({ key: r.key, value: r.value, section: 'profile' }));
  }

  return { rows, saves };
});
