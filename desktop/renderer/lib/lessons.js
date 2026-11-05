/* Teaching Mode (S16) in the panel: the lesson form's check, the saved lessons' rows, the photo check for Check my
 * work, and what its result shows. Pure: data in, plain data out. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LanternLessons = api;
})(typeof self !== 'undefined' ? self : this, function () {
  const PHOTO_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
  const MAX_PHOTO = 15 * 1024 * 1024;

  /** '' when the lesson form can be sent, else what's wrong. */
  function check(request) {
    const r = String(request || '').trim();
    if (!r) return 'Say what you want to learn, for example "solve x² − 5x + 6 = 0".';
    if (r.length > 500) return 'Keep it to a sentence or two.';
    return '';
  }

  /** The saved lessons as rows: title, and how many steps SymPy checked. */
  function rows(list) {
    return (Array.isArray(list) ? list : []).map((l) => {
      const n = Number(l.verified) || 0;
      const checked = l.subject === 'algebra' || l.subject === 'calculus';
      return { name: String(l.name), title: String(l.title || l.name),
               line: checked ? `${n} step${n === 1 ? '' : 's'} checked by SymPy ✓` : 'No ✓: not algebra or calculus' };
    });
  }

  /** '' when the photo can be sent, else what's wrong. */
  function photoCheck(file) {
    if (!file) return 'Choose a photo of your working first.';
    if (!PHOTO_TYPES.includes(file.type)) return 'Send a JPEG, PNG or WebP photo.';
    if (file.size > MAX_PHOTO) return 'That photo is over 15 MB.';
    return '';
  }

  /** What the Check my work card shows for a "checkwork" event: the reply, and the circled photo's name if any. */
  function result(e) {
    if (!e || e.type !== 'checkwork') return null;
    const image = e.image ? String(e.image).split(/[\\/]/).pop() : '';
    return { text: String(e.reply || ''), image: /^checked-[0-9-]+\.png$/.test(image) ? image : '',
             tone: e.wrong ? 'wrong' : e.unreadable ? 'unclear' : 'ok' };
  }

  return { check, rows, photoCheck, result };
});
