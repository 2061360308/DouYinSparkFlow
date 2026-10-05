/* 快速开始：docsify 插件
 * 从 docs/deploy/release.md 拉取内容，按「选择部署方式」确定的场景筛选章节并组装。
 * 依赖 window.DP（docs/static/deploy-shared.js）与 marked（CDN）。
 *
 * release.md 约定：
 *   <!-- when: 条件 --> ... <!-- end -->   条件块；条件语法：
 *       , 同键 OR（os=windows,linux）  ; 组内 AND（os=windows; mode=config）  | 组间 OR（os=windows | mode=config）
 *   紧邻标题前的 when 作为该章节条件；未写 when 视为 all。
 *   <div class="dp-widget" data-widget="download"></div> / deb-install  动态组件占位
 */
(function () {
  var RELEASE_MD = 'deploy/release.md';
  var SCENARIO_KEY = 'dp_scenario';
  var qsState = { accel: '' };
  var cachedMd = null;

  function readScenario() {
    var s = { os: 'windows', mode: 'scheduled' };
    var hash = (typeof location !== 'undefined' && location.hash) || '';
    var qi = hash.indexOf('?');
    var query = qi >= 0 ? hash.slice(qi + 1) : '';
    var hasParams = false;
    if (query) {
      query.split('&').forEach(function (pair) {
        var kv = pair.split('=');
        var k = kv[0];
        var v = decodeURIComponent(kv[1] || '');
        if (k === 'os' && v) { s.os = v; hasParams = true; }
        if (k === 'mode' && v) { s.mode = v; hasParams = true; }
      });
    }
    if (!hasParams) {
      try {
        var raw = localStorage.getItem(SCENARIO_KEY);
        if (raw) {
          var o = JSON.parse(raw);
          if (o.os) s.os = o.os;
          if (o.mode) s.mode = o.mode;
        }
      } catch (e) { /* ignore */ }
    }
    return s;
  }

  function groupMatches(group, scenario) {
    var parts = group.split(';');
    for (var i = 0; i < parts.length; i++) {
      var kv = parts[i].split('=');
      if (kv.length !== 2) continue;
      var key = kv[0].trim();
      var vals = kv[1].split(',').map(function (x) { return x.trim(); });
      if ((key === 'os' || key === 'mode') && vals.indexOf(scenario[key]) === -1) {
        return false;
      }
    }
    return true;
  }

  /* when 语法：| 组间 OR；; 组内 AND；, 同键 OR。例：os=windows | mode=config */
  function whenMatches(when, scenario) {
    if (!when || when === 'all') return true;
    return when.split('|').some(function (group) {
      return groupMatches(group, scenario);
    });
  }

  function parseSections(md) {
    var lines = md.split(/\r?\n/);
    var sections = [];
    var stack = [];
    var cur = null;
    var pendingWhen = null;
    var activeWhen = null;

    function pushBlock(when, line) {
      if (!cur) return;
      var blocks = cur.blocks;
      var last = blocks[blocks.length - 1];
      if (!last || last.when !== when) {
        last = { when: when, lines: [] };
        blocks.push(last);
      }
      last.lines.push(line);
    }

    lines.forEach(function (line) {
      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        var level = h[1].length;
        while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
        var parentWhen = stack.length ? stack[stack.length - 1].when : null;
        var when = pendingWhen || parentWhen;
        cur = { level: level, title: h[2].trim(), when: when, blocks: [] };
        sections.push(cur);
        stack.push({ level: level, when: when });
        pendingWhen = null;
        activeWhen = when;
        return;
      }
      var w = line.match(/^\s*<!--\s*when:\s*(.+?)\s*-->\s*$/);
      if (w) { pendingWhen = w[1].trim(); return; }
      // <!-- end -->：结束内联条件块，回到所属章节自身的 when
      if (/^\s*<!--\s*end\s*-->\s*$/.test(line)) {
        activeWhen = cur ? cur.when : null;
        return;
      }
      if (!cur) return;
      // 空行不消耗 pendingWhen（它可能还在等下一个标题）
      if (line.trim() === '' && pendingWhen) return;
      // 正文里的 when 开启一个条件块，并一直生效到下一个 when / 标题
      if (pendingWhen) {
        activeWhen = pendingWhen;
        pendingWhen = null;
      }
      pushBlock(activeWhen, line);
    });
    return sections;
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderMarkdown(md) {
    if (typeof marked !== 'undefined') {
      if (typeof marked.parse === 'function') return marked.parse(md);
      if (typeof marked === 'function') return marked(md);
    }
    return '<pre>' + escapeHtml(md) + '</pre>';
  }

  function buildHtml(md, scenario) {
    var sections = parseSections(md);
    var out = [];
    sections.forEach(function (sec) {
      if (sec.level < 2) return;
      if (!whenMatches(sec.when, scenario)) return;
      var inner = '';
      sec.blocks.forEach(function (block) {
        if (!whenMatches(block.when, scenario)) return;
        inner += renderMarkdown(block.lines.join('\n'));
      });
      if (inner.trim() === '') return;
      out.push(
        '<section class="qs-section">' +
          '<h' + sec.level + '>' + sec.title + '</h' + sec.level + '>' +
          inner +
        '</section>'
      );
    });
    if (!out.length) out.push('<div class="qs-error">没有匹配当前场景的章节。</div>');
    return out.join('');
  }

  function header(scenario) {
    var osLabel = { windows: 'Windows', linux: 'Linux', macos: 'macOS' }[scenario.os] || scenario.os;
    var modeLabel = {
      scheduled: '常驻定时模式',
      boot: '开机执行模式',
      config: '生成配置（Docker）'
    }[scenario.mode] || scenario.mode;
    return (
      '<div class="qs-scenario">当前场景：<b>' + osLabel + ' · ' + modeLabel + '</b>' +
      '<a href="#/guide/01-选择部署方式">重新选择</a></div>'
    );
  }

  function injectWidgets(mount, scenario) {
    var hasDownload = false;
    mount.querySelectorAll('.dp-widget[data-widget]').forEach(function (el) {
      var name = el.getAttribute('data-widget');
      if (name === 'download') {
        el.innerHTML = DP.buildDownloadBlock({
          targets: DP.packagesForScenario(scenario),
          accel: qsState.accel
        });
        hasDownload = true;
      } else if (name === 'deb-install') {
        el.innerHTML = DP.buildDebInstall();
      } else {
        el.innerHTML = '';
      }
    });
    return hasDownload;
  }

  function renderInto(mount) {
    var scenario = readScenario();
    if (!cachedMd) {
      mount.innerHTML = header(scenario) + '<div class="qs-loading">正在加载使用说明…</div>';
      fetch(RELEASE_MD, { cache: 'no-cache' })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.text();
        })
        .then(function (md) {
          cachedMd = md;
          renderInto(mount);
        })
        .catch(function () {
          mount.innerHTML = header(scenario) +
            '<div class="qs-error">无法加载使用说明（' + RELEASE_MD + '）。</div>';
        });
      return;
    }

    mount.innerHTML = header(scenario) + buildHtml(cachedMd, scenario);
    var hasDownload = injectWidgets(mount, scenario);

    if (hasDownload) {
      var ctx = {
        getAccel: function () { return qsState.accel; },
        setAccel: function (n) { qsState.accel = n; }
      };
      DP.bindAccel(mount, ctx);
      DP.runLatencyOnce(mount, ctx);
    }

    // 内容为异步注入，docsify 的 doneEach 已过，这里再绑定一次图片放大
    if (DP.bindZoom) DP.bindZoom();
  }

  function quickStartPlugin(hook) {
    hook.doneEach(function () {
      var mount = document.getElementById('quick-start');
      if (!mount) return;
      renderInto(mount);
    });
  }

  if (window.DP) {
    DP.onVersionChange(function () {
      var mount = document.getElementById('quick-start');
      if (mount) renderInto(mount);
    });
  }

  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat(quickStartPlugin);
})();
