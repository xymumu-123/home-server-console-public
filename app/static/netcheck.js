/* netcheck.js - 线路判定 + IPv4 备用线路门控/提示 (全站共用)
 * 语义: 当前线路由访问入口唯一确定 (页面能打开就是连通证明, 零探测零误报):
 *   - pc2.jzlspace.top:18002 (IPv6 直连) → 'v6'
 *   - frp-net.com:62809    (IPv4 备用)   → 'v4'
 *   - 其他 (本机/局域网 IP)              → 'lan'
 * - 门控: 仅 current()==='v4' 时游戏/影视受限, 按钮直接跳转 IPv6 通道
 * - 线路状态显示与切换入口在中控页 hero 区 (line-mini), 右下角不再放徽章
 */
(function () {
  'use strict';

  function info() {
    return fetch('/api/probe/info', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  function originOf(url) {
    try { return new URL(url).origin; } catch (e) { return ''; }
  }

  function currentOf(i) {
    if (!i) return 'unknown';
    var o = location.origin;
    if (i.frp_url && o === originOf(i.frp_url)) return 'v4';
    if (i.v6_origin && o === originOf(i.v6_origin)) return 'v6';
    return 'lan';
  }

  /* 跳转到另一条线路的当前路径 (目标 origin 由探针接口下发) */
  function jump(target) {
    if (!target) return false;
    location.href = target + location.pathname + location.search;
    return true;
  }

  /* 单次时延探测 (测速卡片的时延阶段用; 失败返回 null) */
  function ping(url, ms) {
    var c = new AbortController();
    var t = setTimeout(function () { c.abort(); }, ms || 3000);
    var t0 = performance.now();
    return fetch(url + (url.indexOf('?') < 0 ? '?' : '&') + 'ts=' + Date.now(),
      { mode: 'cors', cache: 'no-store', signal: c.signal })
      .then(function () { return Math.round(performance.now() - t0); })
      .catch(function () { return null; })
      .finally(function () { clearTimeout(t); });
  }

  var NC = {
    ready: null,
    ping: ping,
    state: function () {
      try { return JSON.parse(localStorage.getItem('netcheck_v3')); } catch (e) { return null; }
    },
    current: function () {
      var s = NC.state();
      return (s && s.cur) || NC._cur || 'unknown';
    },
    ensure: function () {
      if (!NC.ready) {
        NC.ready = info().then(function (i) {
          var cur = currentOf(i);
          var res = { cur: cur, ts: Date.now() };
          try { localStorage.setItem('netcheck_v3', JSON.stringify(res)); } catch (e) { }
          NC._cur = cur;
          NC._info = i;
          return res;
        });
      }
      return NC.ready;
    },
    recheck: function () { NC.ready = null; NC._cur = null; return NC.ensure(); },
    /* 跳到指定线路 (不传参 = 自动切到另一条) */
    switchLine: async function (to) {
      var cur = NC.current();
      var i = NC._info || await info();
      var target = to === 'v6' ? (i && i.v6_origin)
                 : to === 'v4' ? (i && i.frp_url)
                 : (cur === 'v4' ? (i && i.v6_origin) : (i && i.frp_url));
      return jump(target);
    },
    /* ---------- IPv4 备用线路门控 (frp 线路专用) ---------- */
    gate: function (name) {
      if (document.getElementById('nc-gate')) return;
      var o = document.createElement('div');
      o.id = 'nc-gate';
      o.style.cssText = 'position:fixed;inset:0;z-index:9500;background:rgba(10,16,28,.93);' +
        'display:flex;align-items:center;justify-content:center;padding:24px;';
      o.innerHTML = '<div style="max-width:430px;background:#16233a;border:1px solid #2a3a55;' +
        'border-radius:16px;padding:30px 28px;text-align:center;font:14px/1.9 system-ui,\'Microsoft YaHei\',sans-serif;color:#e8edf5">' +
        '<div style="font-size:12px;letter-spacing:2px;color:#7f92ad;margin-bottom:8px">IPv4 备用线路</div>' +
        '<div style="font-size:16px;font-weight:600;margin-bottom:10px">' + name + '需要 IPv6 网络</div>' +
        '<div style="color:#9fb0c8">当前正通过 IPv4 备用通道访问，' + name +
        '无法正常使用。<br>点击下方按钮切换到 IPv6 通道（可能需要重新登录）。</div>' +
        '<button id="nc-gate-retry" style="margin-top:18px;padding:9px 24px;border-radius:9px;' +
        'border:1px solid #4f8ef7;background:#4f8ef7;color:#fff;font-size:13px;cursor:pointer">切换到 IPv6 通道</button>' +
        '</div>';
      document.body.insertBefore(o, document.body.firstChild);
      o.querySelector('#nc-gate-retry').onclick = async function () {
        this.disabled = true; this.textContent = '跳转中…';
        var i = NC._info || await info();
        if (!(i && i.v6_origin && jump(i.v6_origin))) {
          this.disabled = false; this.textContent = '未配置 IPv6 通道';
        }
      };
    }
  };
  window.NetCheck = NC;

  /* ---------- frp 入口提示横幅 ---------- */
  function frpBanner(s) {
    if (!document.body) { setTimeout(function () { frpBanner(s); }, 100); return; }
    if (s.cur !== 'v4' || !NC._info) return;
    if (document.getElementById('nc-frp-banner')) return;
    try { if (sessionStorage.getItem('nc_frp_banner') === '1') return; } catch (e) { }
    var v6Origin = NC._info.v6_origin || '';
    var b = document.createElement('div');
    b.id = 'nc-frp-banner';
    b.style.cssText = 'position:sticky;top:0;z-index:8500;font:13px/1.6 system-ui,"Microsoft YaHei",sans-serif;' +
      'background:linear-gradient(90deg,#3a2f12,#4a3a15);color:#ffe9a8;border-bottom:1px solid rgba(255,220,120,.25);' +
      'padding:9px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;justify-content:center;';
    b.innerHTML = '<span>当前正通过 IPv4 备用通道访问，上传下载与视频播放受带宽限制。' +
      (v6Origin ? '如果你的网络支持 IPv6，切换到直连通道可获得完整体验（可能需要重新登录）。' : '') + '</span>' +
      (v6Origin
        ? '<button id="nc-frp-switch" style="padding:5px 16px;border-radius:8px;border:1px solid #ffd34d;' +
          'background:#ffd34d;color:#4a3a15;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap">切换到 IPv6 通道</button>'
        : '') +
      '<span id="nc-frp-close" style="cursor:pointer;color:#c9b87a;font-size:15px;padding:0 4px" title="本次关闭">✕</span>';
    document.body.insertBefore(b, document.body.firstChild);
    var sw = document.getElementById('nc-frp-switch');
    if (sw) {
      sw.onclick = function () {
        try { sessionStorage.setItem('nc_frp_banner', '1'); } catch (e) { }
        NC.switchLine('v6');
      };
    }
    document.getElementById('nc-frp-close').onclick = function () {
      try { sessionStorage.setItem('nc_frp_banner', '1'); } catch (e) { }
      b.remove();
    };
  }

  // 立即开始检测 (脚本在 head 也能先跑 fetch; body 未就绪时 render/frpBanner 会自行延迟)
  try { localStorage.removeItem('netcheck_v1'); localStorage.removeItem('netcheck_v2'); } catch (e) { }
  NC.ensure().then(function (s) { render(s); frpBanner(s); });
})();
