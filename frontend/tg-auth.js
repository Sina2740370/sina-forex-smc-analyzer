// به همه‌ی درخواست‌های /api هدر احراز هویت مینی‌اپ تلگرام را اضافه می‌کند.
(function () {
  function getInitData() {
    try {
      var w = window.Telegram && window.Telegram.WebApp;
      if (w && w.initData) return w.initData;
    } catch (e) {}
    try {
      var h = new URLSearchParams(location.hash.slice(1)).get('tgWebAppData');
      if (h) { sessionStorage.setItem('sq_tg_init', h); return h; }
      return sessionStorage.getItem('sq_tg_init') || '';
    } catch (e) { return ''; }
  }

  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var u = new URL(url, location.origin);
      var data = getInitData();
      if (data && u.origin === location.origin && u.pathname.indexOf('/api/') === 0) {
        init = init || {};
        var headers = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
        if (!headers.has('Authorization')) headers.set('Authorization', 'tma ' + data);
        init.headers = headers;
      }
    } catch (e) {}
    return origFetch.call(this, input, init);
  };
})();
        
