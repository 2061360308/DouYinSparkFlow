/* 发行包使用（release.md）独立页的动态组件
 * 把 release.md 里的 .dp-widget 占位渲染为「全部」下载链接 / deb 安装命令。
 * 快速开始页（#quick-start）内的组件由 quick-start.js 负责，这里跳过。
 * 依赖 window.DP（docs/static/deploy-shared.js）。
 */
(function () {
  var state = { accel: '' };

  function renderWidgets() {
    if (!window.DP) return;
    var qs = document.getElementById('quick-start');
    var scope = document.querySelector('.markdown-section') || document.body;

    scope.querySelectorAll('.dp-widget[data-widget]').forEach(function (el) {
      if (qs && qs.contains(el)) return;
      var name = el.getAttribute('data-widget');
      if (name === 'download') {
        el.innerHTML = DP.buildDownloadBlock({ targets: DP.allPackages(), accel: state.accel });
        var ctx = {
          getAccel: function () { return state.accel; },
          setAccel: function (n) { state.accel = n; }
        };
        DP.bindAccel(el, ctx);
        DP.runLatencyOnce(el, ctx);
      } else if (name === 'deb-install') {
        el.innerHTML = DP.buildDebInstall();
      } else {
        el.innerHTML = '';
      }
    });
  }

  function releaseWidgetPlugin(hook) {
    hook.doneEach(renderWidgets);
  }

  if (window.DP) {
    DP.onVersionChange(renderWidgets);
  }

  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat(releaseWidgetPlugin);
})();
