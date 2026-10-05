/* 选择部署方式：docsify 插件
 * 依赖 docs/guide/01-选择部署方式.md 里的 #deploy-picker 结构。
 * 通过 window.$docsify.plugins 注册，必须在 docsify.min.js 之前加载。
 */
(function () {
  var SCENARIO_KEY = 'dp_scenario';

  function modeOf(state) {
    if (state.desktop === 'no' || state.os === 'macos' || state.method === 'docker') return 'config';
    return state.always === 'yes' ? 'scheduled' : 'boot';
  }

  function deployPickerPlugin(hook) {
    hook.doneEach(function () {
      var root = document.getElementById('deploy-picker');
      if (!root || root.dataset.bound === '1') return;
      root.dataset.bound = '1';

      var state = { os: 'windows', always: 'yes', desktop: 'yes', method: '' };
      var groups = {
        os: root.querySelector('[data-group="os"]'),
        always: root.querySelector('[data-group="always"]'),
        desktop: root.querySelector('[data-group="desktop"]')
      };
      var wraps = {
        always: root.querySelector('[data-group-wrap="always"]'),
        desktop: root.querySelector('[data-group-wrap="desktop"]')
      };
      var result = document.getElementById('dp-result');

      function setCardLocked(groupName, value, locked) {
        groups[groupName].querySelectorAll('.dp-card').forEach(function (btn) {
          if (btn.dataset.value === value) {
            btn.classList.toggle('is-locked', locked);
          }
        });
      }

      function methodCard(method) {
        var active = state.method === method ? ' is-active' : '';
        var title;
        var desc;
        if (method === 'docker') {
          title = 'Docker 部署';
          desc = '容器化部署，出口 IP 固定、环境统一；适合开发者与统一运维';
        } else if (state.always === 'yes') {
          title = '发行包 · 常驻模式';
          desc = '图形化安装，注册系统定时任务，到点自动执行；适合新手，操作简单';
        } else {
          title = '发行包 · 开机执行模式';
          desc = '图形化安装，开机自动补跑当天任务；适合新手，操作简单';
        }
        return (
          '<button type="button" class="dp-method' + active + '" data-method="' + method + '">' +
            '<span class="dp-method-title">' + title + '</span>' +
            '<span class="dp-method-desc">' + desc + '</span>' +
          '</button>'
        );
      }

      function stepSection(method) {
        var text = method === 'docker'
          ? 'Docker 部署'
          : (state.always === 'yes' ? '常驻模式' : '开机执行模式');
        return (
          '<a class="dp-step-link" href="#/guide/快速开始">' +
            '<span class="dp-step-badge">下一步</span>' +
            '<span class="dp-step-text">' + text + '</span>' +
            '<span class="dp-link-arrow">\u2192</span>' +
          '</a>'
        );
      }

      function persistScenario() {
        try {
          localStorage.setItem(SCENARIO_KEY, JSON.stringify({
            os: state.os,
            always: state.always,
            desktop: state.desktop,
            method: state.method,
            mode: modeOf(state)
          }));
        } catch (e) {
          /* ignore */
        }
      }

      function render() {
        var mac = state.os === 'macos';

        // 「关机」与「无桌面」互斥：任一被选中，另一项自动切回并锁定
        if (!mac) {
          if (state.always === 'no') state.desktop = 'yes';
          if (state.desktop === 'no') state.always = 'yes';
        }

        wraps.always.classList.toggle('is-disabled', mac);
        wraps.desktop.classList.toggle('is-disabled', mac);

        Object.keys(groups).forEach(function (g) {
          groups[g].querySelectorAll('.dp-card').forEach(function (btn) {
            btn.classList.toggle('is-active', btn.dataset.value === state[g]);
            btn.classList.remove('is-locked');
          });
        });

        if (!mac) {
          setCardLocked('desktop', 'no', state.always === 'no');
          setCardLocked('always', 'no', state.desktop === 'no');
        }

        var isAlways = state.always === 'yes';
        var pkgAvailable = !mac && state.desktop === 'yes';
        var dockerAvailable = mac || isAlways;

        var methods = [];
        if (pkgAvailable) methods.push('pkg');
        if (dockerAvailable) methods.push('docker');
        if (methods.indexOf(state.method) === -1) state.method = methods[0] || '';

        var methodCards = methods.map(methodCard).join('');

        result.innerHTML =
          '<div class="dp-result-head"><span class="dp-step">推荐</span>可用部署方式</div>' +
          '<div class="dp-methods">' + methodCards + '</div>' +
          (state.method ? stepSection(state.method) : '');

        result.querySelectorAll('.dp-method').forEach(function (btn) {
          btn.addEventListener('click', function () {
            state.method = btn.dataset.method;
            render();
          });
        });

        persistScenario();
      }

      root.querySelectorAll('.dp-card').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var group = btn.closest('.dp-options').dataset.group;
          state[group] = btn.dataset.value;
          render();
        });
      });

      render();
    });
  }

  window.$docsify = window.$docsify || {};
  window.$docsify.plugins = (window.$docsify.plugins || []).concat(deployPickerPlugin);
})();
