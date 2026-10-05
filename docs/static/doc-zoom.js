/* 正文图片点击放大（medium-zoom）
 * - 在 docsify 每次渲染后绑定 .markdown-section 里的图片；
 * - 暴露 window.DP.bindZoom()，供快速开始（异步注入内容）在注入后再绑定一次。
 * 依赖 window.DP（docs/static/deploy-shared.js）与 medium-zoom（CDN）。
 */
(function () {
  var zoom = null;

  function isInsideLink(el) {
    var p = el.parentElement;
    while (p) {
      if (p.tagName === 'A') return true;
      p = p.parentElement;
    }
    return false;
  }

  function bindZoom() {
    if (typeof mediumZoom !== 'function') return;
    var imgs = Array.prototype.slice
      .call(document.querySelectorAll('.markdown-section img:not(.emoji)'))
      .filter(function (el) { return !isInsideLink(el); });
    if (zoom) {
      try { zoom.detach(); } catch (e) {}
    }
    zoom = mediumZoom(imgs);
  }

  function docZoomPlugin(hook) {
    hook.doneEach(bindZoom);
  }

  if (window.DP) window.DP.bindZoom = bindZoom;

  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat(docZoomPlugin);
})();
