// City Permit Portal: small progressive enhancements. The form works without this file.
(function () {
  // Card fields only matter for card payment; hide them otherwise (they stay in the page for testing).
  const card = document.getElementById('card-fields');
  if (card) {
    const radios = Array.from(document.querySelectorAll('.pay-method input[type=radio]'));
    const sync = () => { card.hidden = !radios.some((r) => r.checked && r.value === 'card'); };
    radios.forEach((r) => r.addEventListener('change', sync));
    sync();
  }
  // One click, one submission.
  document.querySelectorAll('form').forEach((f) => f.addEventListener('submit', () => {
    setTimeout(() => f.querySelectorAll('button[type=submit]').forEach((b) => { b.disabled = true; }), 0);
  }));
})();
