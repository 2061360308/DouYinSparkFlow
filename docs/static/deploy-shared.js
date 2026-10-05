/* 共享模块：版本获取、加速节点下拉、节点测速、发行包下载块
 * 供 deploy-picker.js（选择部署方式）与 quick-start.js（快速开始）复用。
 * 挂载到 window.DP，必须在 docsify.min.js 之前加载。
 */
(function () {
  var REPO = '2061360308/DouYinSparkFlow';
  var FALLBACK_VERSION = 'v3.3.1';
  var version = FALLBACK_VERSION;
  var versionSettled = false;
  var versionIsFallback = false;
  var versionCallbacks = [];

  /* 加速节点：格式为 https://<节点>/<原始链接>（与 github.akams.cn 一致） */
  var PROXIES = [
    'ghproxy.net',
    'gh-proxy.com',
    'cdn.gh-proxy.com',
    'ghfast.top',
    'gh.ddlc.top',
    'ghproxy.icu',
    'github.chenc.dev',
    'gitproxy.click',
    'github.geekery.cn',
    'gh.tryxd.cn',
    'gh.sixyin.com',
    'gh.jasonzeng.dev',
    'fastgit.cc',
    'ghproxy.imciel.com',
    'ghproxy.monkeyray.net',
    'gh.idayer.com',
    'github.ednovas.xyz',
    'ghp.keleyaa.com',
    'gitproxy.mrhjx.cn',
    'github-proxy.memory-echoes.cn',
    'ghp.arslantu.xyz',
    'gitproxy.127731.xyz',
    'gh.catmak.name',
    'gh.acmsz.top'
  ];
  /* 首位为空串，代表「无（直连）」 */
  var NODES = [''].concat(PROXIES);

  var LATENCY_IMAGES = [
    'https://raw.githubusercontent.com/microsoft/terminal/refs/heads/main/res/terminal/images/SmallTile.scale-125.png',
    'https://raw.githubusercontent.com/microsoft/vscode/refs/heads/main/resources/linux/code.png',
    'https://raw.githubusercontent.com/facebook/react/refs/heads/main/fixtures/dom/public/favicon.ico',
    'https://raw.githubusercontent.com/python/cpython/refs/heads/main/PC/icons/python.ico'
  ];
  var LATENCY_TIMEOUT = 5000;
  var LATENCY_CACHE_KEY = 'dp_node_latency';
  var LATENCY_TTL = 10 * 60 * 1000;

  var SPEED_FILES = [
    'https://github.com/microsoft/terminal/releases/download/v1.22.10731.0/Microsoft.WindowsTerminal_1.22.10731.0_x64.zip',
    'https://github.com/pypa/pip/archive/refs/tags/24.3.1.zip'
  ];
  var SPEED_MAX_BYTES = 1.5 * 1024 * 1024;
  var SPEED_MAX_MS = 6000;
  var SPEED_CACHE_KEY = 'dp_node_speed';
  var SPEED_TTL = 10 * 60 * 1000;

  function loadCache(key, ttl) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return {};
      var obj = JSON.parse(raw);
      var now = Date.now();
      var out = {};
      Object.keys(obj).forEach(function (k) {
        if (obj[k] && now - obj[k].timestamp < ttl) out[k] = obj[k].value;
      });
      return out;
    } catch (e) {
      return {};
    }
  }

  function saveCache(key, cache) {
    try {
      var now = Date.now();
      var obj = {};
      Object.keys(cache).forEach(function (k) {
        obj[k] = { value: cache[k], timestamp: now };
      });
      localStorage.setItem(key, JSON.stringify(obj));
    } catch (e) {
      /* 隐私模式等场景忽略 */
    }
  }

  var latencyCache = loadCache(LATENCY_CACHE_KEY, LATENCY_TTL);
  var speedCache = loadCache(SPEED_CACHE_KEY, SPEED_TTL);
  var autoLatencyDone = false;

  function notifyVersion() {
    var cbs = versionCallbacks;
    versionCallbacks = [];
    cbs.forEach(function (cb) {
      try { cb(version); } catch (e) {}
    });
  }

  /* tag 为空表示所有来源都失败，保留内置版本并标记「已结束」 */
  function setVersion(tag) {
    if (tag) {
      version = tag;
      versionIsFallback = false;
    } else {
      versionIsFallback = true;
    }
    versionSettled = true;
    notifyVersion();
  }

  var API_PATH = '/repos/' + REPO + '/releases/latest';
  /* 直连失败时依次走加速节点代理 API（与 github.akams.cn 同思路） */
  var API_BASES = [
    'https://api.github.com',
    'https://gh.dpik.top/https://api.github.com',
    'https://gh-proxy.com/https://api.github.com',
    'https://ghfast.top/https://api.github.com'
  ];

  function fetchLatestVersion() {
    if (typeof fetch !== 'function') { setVersion(null); return; }
    var bases = API_BASES.slice();
    (function attempt() {
      if (!bases.length) { setVersion(null); return; }
      var base = bases.shift();
      var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (controller) controller.abort(); }, 8000);
      fetch(base + API_PATH, {
        headers: { Accept: 'application/vnd.github+json' },
        signal: controller ? controller.signal : undefined
      })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          clearTimeout(timer);
          if (data && data.tag_name) setVersion(data.tag_name);
          else attempt();
        })
        .catch(function () {
          clearTimeout(timer);
          attempt();
        });
    })();
  }

  function onVersionChange(cb) {
    if (versionSettled) {
      try { cb(version); } catch (e) {}
      return;
    }
    versionCallbacks.push(cb);
  }

  function accelUrl(raw, node) {
    return node ? 'https://' + node + '/' + raw : raw;
  }

  function metricsLabel(node) {
    var seg = [];
    var ms = latencyCache[node];
    if (ms !== undefined) seg.push(ms >= LATENCY_TIMEOUT ? '超时' : ms + 'ms');
    var sp = speedCache[node];
    if (sp !== undefined) seg.push(sp > 0 ? sp.toFixed(1) + 'Mbps' : '测速失败');
    return seg.length ? '（' + seg.join(' · ') + '）' : '';
  }

  function metricParts(node) {
    var ms = latencyCache[node];
    var latClass = 'dp-node-metric dp-latency';
    var latText = '—';
    if (ms !== undefined) {
      if (ms >= LATENCY_TIMEOUT) {
        latText = '超时';
        latClass += ' is-bad';
      } else {
        latText = ms + 'ms';
        latClass += ms < 1000 ? ' is-good' : ' is-bad';
      }
    }
    var v = speedCache[node];
    var spClass = 'dp-node-metric dp-speed';
    var spText = '—';
    if (v !== undefined) {
      if (!v || v <= 0) {
        spText = '失败';
        spClass += ' is-fail';
      } else {
        spText = v.toFixed(1) + 'Mbps';
        spClass += ' is-speed';
      }
    }
    return { latClass: latClass, latText: latText, spClass: spClass, spText: spText };
  }

  function measureLatency(node) {
    return new Promise(function (resolve) {
      var img = new Image();
      var start = performance.now();
      var done = false;
      var timer = setTimeout(function () {
        if (done) return;
        done = true;
        img.src = '';
        resolve(LATENCY_TIMEOUT);
      }, LATENCY_TIMEOUT);
      img.onload = function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(Math.round(performance.now() - start));
      };
      img.onerror = function () {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(LATENCY_TIMEOUT);
      };
      var testImg = LATENCY_IMAGES[Math.floor(Math.random() * LATENCY_IMAGES.length)];
      img.src = accelUrl(testImg, node) + '?t=' + Date.now();
    });
  }

  /* 只读前 SPEED_MAX_BYTES / SPEED_MAX_MS，按实际字节数换算 Mbps */
  function measureSpeed(node) {
    return new Promise(function (resolve) {
      var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var file = SPEED_FILES[Math.floor(Math.random() * SPEED_FILES.length)];
      var url = accelUrl(file, node) + '?t=' + Date.now();
      var start = performance.now();
      var bytes = 0;
      var settled = false;

      function done() {
        if (settled) return;
        settled = true;
        var secs = (performance.now() - start) / 1000;
        if (bytes > 0 && secs > 0) {
          resolve(((bytes * 8) / secs / 1e6).toFixed(1) + 'Mbps');
        } else {
          resolve('-');
        }
      }

      var timer = setTimeout(function () {
        if (controller) controller.abort();
        done();
      }, SPEED_MAX_MS);

      fetch(url, { cache: 'no-cache', signal: controller ? controller.signal : undefined })
        .then(function (res) {
          if (!res.ok || !res.body || !res.body.getReader) {
            clearTimeout(timer);
            done();
            return;
          }
          var reader = res.body.getReader();
          (function pump() {
            reader.read().then(function (r) {
              if (r.done) {
                clearTimeout(timer);
                done();
                return;
              }
              bytes += r.value.length;
              if (bytes >= SPEED_MAX_BYTES) {
                try { reader.cancel(); } catch (e) {}
                clearTimeout(timer);
                done();
                return;
              }
              pump();
            }).catch(function () {
              clearTimeout(timer);
              done();
            });
          })();
        })
        .catch(function () {
          clearTimeout(timer);
          done();
        });
    });
  }

  function runWithConcurrency(items, limit, worker) {
    return new Promise(function (resolve) {
      var results = new Array(items.length);
      var next = 0;
      var active = 0;
      var done = 0;
      if (!items.length) {
        resolve(results);
        return;
      }
      function pump() {
        while (active < limit && next < items.length) {
          var idx = next++;
          active++;
          worker(items[idx]).then(function (r) {
            results[idx] = r;
          }).catch(function () {}).then(function () {
            active--;
            done++;
            if (done === items.length) resolve(results);
            else pump();
          });
        }
      }
      pump();
    });
  }

  function fastestNode() {
    var best = null;
    PROXIES.forEach(function (node) {
      var ms = latencyCache[node];
      if (ms !== undefined && ms < LATENCY_TIMEOUT && (best === null || ms < latencyCache[best])) {
        best = node;
      }
    });
    return best;
  }

  function buildNodeRow(node, label, accel) {
    var m = metricParts(node);
    var active = accel === node ? ' is-active' : '';
    return (
      '<button type="button" class="dp-node' + active + '" data-node="' + node + '">' +
        '<span class="dp-node-name">' + (label || node) + '</span>' +
        '<span class="' + m.latClass + '">' + m.latText + '</span>' +
        '<span class="' + m.spClass + '">' + m.spText + '</span>' +
      '</button>'
    );
  }

  function buildNodePanel(accel) {
    var rows = [buildNodeRow('', '无（直连）', accel)].concat(
      PROXIES.map(function (n) { return buildNodeRow(n, n, accel); })
    );
    return (
      '<div class="dp-select-panel">' +
        '<div class="dp-nodes-head"><span>节点</span><span>延迟</span><span>速率</span></div>' +
        '<div class="dp-node-list">' + rows.join('') + '</div>' +
      '</div>'
    );
  }

  /* opts: { targets:[{name,url,badge}], note, accel } */
  function buildDownloadBlock(opts) {
    var accel = opts.accel || '';
    var links = (opts.targets || []).map(function (t) {
      var href = accelUrl(t.url, accel);
      var badge = t.badge ? '<span class="dp-current-badge">' + t.badge + '</span>' : '';
      return (
        '<a class="dp-download-link" data-raw="' + t.url + '" href="' + href + '">' +
          '<span class="dp-download-name">' + t.name + '</span>' +
          badge +
          '<span class="dp-download-arrow">\u2193</span>' +
        '</a>'
      );
    }).join('');
    var m = metricParts(accel);
    return (
      '<div class="dp-download">' +
        '<div class="dp-download-head">' +
          '<span class="dp-download-title">' + (opts.title || '下载最新发行包') + '</span>' +
          '<span class="dp-version"' + (versionIsFallback ? ' title="无法获取最新版本，显示内置版本"' : '') + '>' + version + (versionSettled ? '' : '（获取中…）') + '</span>' +
          '<div class="dp-accel">' +
            '<span class="dp-accel-label">加速下载</span>' +
            '<div class="dp-select">' +
              '<button type="button" class="dp-select-trigger">' +
                '<span class="dp-select-value">' + (accel || '无（直连）') + '</span>' +
                '<span class="' + m.latClass + '">' + m.latText + '</span>' +
                '<span class="' + m.spClass + '">' + m.spText + '</span>' +
                '<span class="dp-select-caret">\u25be</span>' +
              '</button>' +
              buildNodePanel(accel) +
            '</div>' +
            '<button type="button" class="dp-speedtest" data-action="all">节点测速</button>' +
          '</div>' +
        '</div>' +
        '<div class="dp-download-links">' + links + '</div>' +
        (opts.note ? '<div class="dp-note">' + opts.note + '</div>' : '') +
        '<div class="dp-speed-status"></div>' +
      '</div>'
    );
  }

  function refreshTrigger(rootEl, accel) {
    var val = rootEl.querySelector('.dp-select-value');
    if (val) val.textContent = accel || '无（直连）';
    var m = metricParts(accel);
    var lat = rootEl.querySelector('.dp-select-trigger .dp-latency');
    var sp = rootEl.querySelector('.dp-select-trigger .dp-speed');
    if (lat) {
      lat.className = m.latClass;
      lat.textContent = m.latText;
    }
    if (sp) {
      sp.className = m.spClass;
      sp.textContent = m.spText;
    }
  }

  function updateNodeRow(rootEl, node) {
    var row = rootEl.querySelector('.dp-node[data-node="' + node + '"]');
    if (!row) return;
    var m = metricParts(node);
    var lat = row.querySelector('.dp-latency');
    var sp = row.querySelector('.dp-speed');
    if (lat) {
      lat.className = m.latClass;
      lat.textContent = m.latText;
    }
    if (sp) {
      sp.className = m.spClass;
      sp.textContent = m.spText;
    }
  }

  function applyAccel(rootEl, ctx, node) {
    ctx.setAccel(node);
    rootEl.querySelectorAll('.dp-download-link').forEach(function (a) {
      a.setAttribute('href', accelUrl(a.getAttribute('data-raw'), node));
    });
    rootEl.querySelectorAll('.dp-node').forEach(function (row) {
      row.classList.toggle('is-active', row.getAttribute('data-node') === node);
    });
    refreshTrigger(rootEl, node);
  }

  function runTest(rootEl, btn, ctx, withSpeed) {
    if (btn.dataset.running === '1') return;
    btn.dataset.running = '1';
    btn.disabled = true;
    btn.textContent = '测速中…';

    var status = rootEl.querySelector('.dp-speed-status');
    var total = withSpeed ? NODES.length * 2 : NODES.length;
    var step = 0;
    function tick() {
      step++;
      if (status) status.textContent = '测速中 ' + Math.min(step, total) + '/' + total + ' …';
    }

    runWithConcurrency(NODES, 6, function (node) {
      return measureLatency(node).then(function (ms) {
        latencyCache[node] = ms;
        updateNodeRow(rootEl, node);
        tick();
        return ms;
      });
    }).then(function () {
      saveCache(LATENCY_CACHE_KEY, latencyCache);
      if (!withSpeed) return null;
      return runWithConcurrency(NODES, 3, function (node) {
        return measureSpeed(node).then(function (out) {
          var v = parseFloat(out);
          speedCache[node] = isNaN(v) ? 0 : v;
          updateNodeRow(rootEl, node);
          tick();
          return v;
        });
      }).then(function () {
        saveCache(SPEED_CACHE_KEY, speedCache);
      });
    }).then(function () {
      var best = fastestNode();
      if (best) applyAccel(rootEl, ctx, best);
      btn.disabled = false;
      btn.dataset.running = '0';
      btn.textContent = '节点测速';
      if (status) {
        status.textContent = best
          ? '最快节点：' + best + ' ' + metricsLabel(best)
          : '测速完成';
      }
    });
  }

  /* ctx: { getAccel(), setAccel(node) } */
  function bindAccel(rootEl, ctx) {
    var dd = rootEl.querySelector('.dp-select');
    var trigger = rootEl.querySelector('.dp-select-trigger');
    if (trigger && dd) {
      trigger.addEventListener('click', function () {
        dd.classList.toggle('is-open');
      });
    }
    rootEl.querySelectorAll('.dp-node').forEach(function (row) {
      row.addEventListener('click', function () {
        applyAccel(rootEl, ctx, row.getAttribute('data-node'));
        if (dd) dd.classList.remove('is-open');
      });
    });
    var btn = rootEl.querySelector('.dp-speedtest');
    if (btn) {
      btn.addEventListener('click', function () {
        runTest(rootEl, btn, ctx, true);
      });
    }
    refreshTrigger(rootEl, ctx.getAccel());
  }

  /* 每次页面加载只自动测一次延迟（不测速率） */
  function runLatencyOnce(rootEl, ctx) {
    if (autoLatencyDone) return;
    var btn = rootEl.querySelector('.dp-speedtest');
    if (!btn) return;
    autoLatencyDone = true;
    runTest(rootEl, btn, ctx, false);
  }

  function debName(arch) {
    return 'DouyinSparkFlow_' + version.replace(/^v/, '') + '_' + arch + '.deb';
  }

  function detectSystem() {
    var ua = (typeof navigator !== 'undefined' && navigator.userAgent) || '';
    var os = 'windows';
    if (/Macintosh|Mac OS X/i.test(ua)) os = 'macos';
    else if (/Windows/i.test(ua)) os = 'windows';
    else if (/Android/i.test(ua)) os = 'android';
    else if (/Linux|X11/i.test(ua)) os = 'linux';
    var arch = /aarch64|arm64|armv8/i.test(ua) ? 'arm64' : 'amd64';
    return { os: os, arch: arch };
  }

  function isCurrentPackage(pkg, cur) {
    if (pkg.os !== cur.os) return false;
    if (pkg.os === 'windows') return true;
    return pkg.arch === cur.arch;
  }

  function basePackageList() {
    var ver = version.replace(/^v/, '');
    var base = 'https://github.com/' + REPO + '/releases/download/' + version + '/';
    return [
      { name: 'DouyinSparkFlow-win-x64.zip', url: base + 'DouyinSparkFlow-win-x64.zip', os: 'windows', arch: 'amd64' },
      { name: 'DouyinSparkFlow_' + ver + '_amd64.deb', url: base + 'DouyinSparkFlow_' + ver + '_amd64.deb', os: 'linux', arch: 'amd64' },
      { name: 'DouyinSparkFlow_' + ver + '_arm64.deb', url: base + 'DouyinSparkFlow_' + ver + '_arm64.deb', os: 'linux', arch: 'arm64' }
    ];
  }

  function withBadges(list) {
    var cur = detectSystem();
    return list.map(function (p) {
      return { name: p.name, url: p.url, badge: isCurrentPackage(p, cur) ? '当前系统' : '' };
    });
  }

  function allPackages() {
    return withBadges(basePackageList());
  }

  function packagesForScenario(scenario) {
    var list = basePackageList();
    var filtered;
    if (!scenario || scenario.mode === 'config' || scenario.os === 'macos') {
      filtered = list;
    } else if (scenario.os === 'windows') {
      filtered = list.filter(function (p) { return p.os === 'windows'; });
    } else if (scenario.os === 'linux') {
      filtered = list.filter(function (p) { return p.os === 'linux'; });
    } else {
      filtered = list;
    }
    return withBadges(filtered);
  }

  function buildDebInstall() {
    var v = version.replace(/^v/, '');
    var lines = [
      '# amd64（Intel / AMD）',
      'sudo apt install ./DouyinSparkFlow_' + v + '_amd64.deb',
      '',
      '# arm64（ARM）',
      'sudo apt install ./DouyinSparkFlow_' + v + '_arm64.deb'
    ];
    return '<pre><code class="language-bash">' + lines.join('\n') + '</code></pre>';
  }

  fetchLatestVersion();

  window.DP = {
    REPO: REPO,
    getVersion: function () { return version; },
    isVersionResolved: function () { return versionSettled; },
    isVersionFallback: function () { return versionIsFallback; },
    onVersionChange: onVersionChange,
    accelUrl: accelUrl,
    metricParts: metricParts,
    metricsLabel: metricsLabel,
    buildDownloadBlock: buildDownloadBlock,
    bindAccel: bindAccel,
    runLatencyOnce: runLatencyOnce,
    debName: debName,
    detectSystem: detectSystem,
    isCurrentPackage: isCurrentPackage,
    allPackages: allPackages,
    packagesForScenario: packagesForScenario,
    buildDebInstall: buildDebInstall
  };
})();
