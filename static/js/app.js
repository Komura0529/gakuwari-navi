// 学割ナビ: お気に入り機能（LocalStorage）
(function () {
  const KEY = 'gakuwari_favorites';

  function load() {
    try {
      return JSON.parse(localStorage.getItem(KEY) || '[]');
    } catch (e) {
      return [];
    }
  }

  function save(ids) {
    localStorage.setItem(KEY, JSON.stringify(ids));
  }

  function refresh() {
    const ids = load();
    document.querySelectorAll('.fav-btn').forEach(function (btn) {
      const on = ids.includes(btn.dataset.favId);
      btn.classList.toggle('fav-on', on);
    });
  }

  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.fav-btn');
    if (!btn) return;
    e.preventDefault();
    const id = btn.dataset.favId;
    let ids = load();
    if (ids.includes(id)) {
      ids = ids.filter(function (x) { return x !== id; });
    } else {
      ids.push(id);
    }
    save(ids);
    refresh();
  });

  refresh();
})();
