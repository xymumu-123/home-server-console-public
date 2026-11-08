/* ============================================================
   共享视频播放器 openVideoPlayer(path, opts) —— Video.js 版
   opts: { startAt: 秒(续播起点), onProgress(pos, dur): 进度回调 }
   设计: Video.js 接管控制条/手势/全屏/自动隐藏; 自定义层只保留
         转码逻辑 (真实时长注入 + 拖动重开流) 和进度上报
   依赖: theme.css, video-js.min.css, video.min.js, 同源登录态
   ============================================================ */
(function () {
  let v = null, player = null, base = '', info = { mode: 'direct', duration: 0 };
  let offset = 0, opts = null, lastReport = 0;

  const CSS = `
  .vmodal { position: fixed; inset: 0; background: rgba(0,0,0,.88); z-index: 250;
            display: none; align-items: center; justify-content: center; padding: 20px; }
  .vmodal.show { display: flex; }
  .v-close { position: fixed; top: 14px; right: 18px; width: 38px; height: 38px; border-radius: 50%;
             background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.25);
             color: #fff; cursor: pointer; font-size: 16px; z-index: 5; }
  .player { width: min(1200px, 96vw, 135vh); position: relative; background: #000;
            border-radius: 12px; overflow: hidden; }
  .player .video-js { width: 100%; max-height: 82vh; }
  /* 音量滑块强制常驻展开 (Video.js 默认悬停才展开, 手机上没有 hover 会永远收缩) */
  .player .video-js .vjs-volume-panel .vjs-volume-control,
  .player .video-js .vjs-volume-panel:hover .vjs-volume-control,
  .player .video-js .vjs-volume-panel .vjs-volume-control:active {
    width: 5em !important; opacity: 1 !important; visibility: visible !important;
  }
  .player .video-js .vjs-volume-bar { width: 5em !important; }
  /* 面板宽度跟随内容: 滑块常驻展开后, 固定 4em 的面板盒子会让滑块
     溢出压到右侧进度条上 (桌面端实测重叠 50px) */
  .player .video-js .vjs-volume-panel { width: auto !important; padding-right: .4em; }
  .player:fullscreen { width: 100vw; height: 100vh; border-radius: 0; }
  .player:fullscreen .video-js { max-height: 100vh; height: 100vh; }
  /* 手机上隐藏音量控件: 音量用设备音量键, 桌面保留滑块 */
  @media (max-width: 640px) {
    .player .video-js .vjs-volume-panel { display: none !important; }
  }
  .v-mode-badge { position: absolute; top: 10px; right: 12px; z-index: 10; font-size: 11px;
                  padding: 3px 9px; border-radius: 6px; background: rgba(255,255,255,.18);
                  color: rgba(255,255,255,.92); pointer-events: none; backdrop-filter: blur(2px); }
  `;

  function ensureDom() {
    if (document.getElementById('vmodal')) return;
    const st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);
    const div = document.createElement('div');
    div.id = 'vmodal';
    div.className = 'vmodal';
    div.innerHTML = `<button class="v-close" title="关闭">✕</button><div id="v-body"></div>`;
    div.addEventListener('click', e => { if (e.target === div) close(); });
    div.querySelector('.v-close').onclick = close;
    document.body.appendChild(div);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  }

  const realDur = () => info.duration || (player ? player.duration() : 0) || 0;
  const realCur = () => offset + (player ? player.currentTime() : 0);

  function report(force) {
    if (!opts || !opts.onProgress || !player) return;
    const now = Date.now();
    if (!force && now - lastReport < 5000) return;
    lastReport = now;
    opts.onProgress(realCur(), realDur());
  }

  function killPlayer() {
    if (!player) return;
    report(true);                        // 关闭前把最终进度记下来
    try { player.pause(); } catch (e) {}
    try { player.dispose(); } catch (e) {}
    player = null; v = null;
  }

  function close() {
    killPlayer();
    const m = document.getElementById('vmodal');
    if (m) { m.classList.remove('show'); document.getElementById('v-body').innerHTML = ''; }
  }

  function toggleFs() {
    if (!player) return;
    // iPhone/iPod: 调起系统原生播放器
    if (/iPhone|iPod/.test(navigator.userAgent) && v.webkitEnterFullscreen) {
      v.webkitEnterFullscreen();
      return;
    }
    if (player.isFullscreen()) player.exitFullscreen();
    else player.requestFullscreen();     // Video.js 处理前缀/iOS 兜底/控制条显隐
  }

  async function openVideoPlayer(path, o) {
    opts = o || {};
    ensureDom();
    base = '/api/fs/stream?path=' + encodeURIComponent(path);
    info = { mode: 'direct', duration: 0 };
    try {
      const r = await fetch(base + '&info=1');
      if (r.ok) info = await r.json();
    } catch (e) {}
    const tr = info.mode !== 'direct';

    // 续播起点有效性 (ffprobe 时长已知时才判断)
    let start = opts.startAt || 0;
    if (!(start > 10 && info.duration && start < info.duration * 0.95)) start = 0;
    if (tr) offset = start;              // 转码: 从 start 位置重开流

    document.getElementById('v-body').innerHTML = `<div class="player">
      <video id="pv" class="video-js" playsinline webkit-playsinline x5-video-player-type="h5"></video>
      <span class="v-mode-badge">${tr ? '转码播放' : '原始直连'}</span>
    </div>`;
    document.getElementById('vmodal').classList.add('show');

    v = document.getElementById('pv');
    player = videojs(v, {
      controls: true,
      autoplay: true,
      preload: 'auto',
      fluid: true,
      playsinline: true,
      playbackRates: [0.5, 0.75, 1, 1.25, 1.5, 2],   // 倍速菜单 (内置组件)
      controlBar: {
        pictureInPictureToggle: false,
        volumePanel: { inline: true }                 // 手机上强制显示音量滑块 (默认只显示静音)
      }
    });

    // ---- 转码流的真实时长/真实位置注入 (社区标准做法) ----
    if (tr && info.duration) {
      player.duration = function () { return info.duration; };
      player.seekable = function () { return videojs.createTimeRanges([[0, info.duration]]); };
      const origCT = player.currentTime.bind(player);
      player.currentTime = function (t) {
        if (typeof t === 'number') {     // 用户拖动: 绝对时间 → 重开转码流
          offset = t;
          v.src = base + '&ss=' + t.toFixed(1);
          origCT(0);
          player.play();
          return t;
        }
        return offset + origCT();
      };
    }

    player.on('loadedmetadata', () => {
      if (!info.duration) info.duration = v.duration || 0;
      if (start && !tr) player.currentTime(start);
    });
    player.on('timeupdate', () => report(false));
    player.on('ended', () => report(true));
    player.on('pause', () => report(true));

    // 手机全屏时按视频画面比例锁横屏 (横屏片才锁, 竖屏剧保持竖屏)
    player.on('fullscreenchange', () => {
      if (player.isFullscreen()) {
        if (v.videoWidth > v.videoHeight && /Mobi|Android/i.test(navigator.userAgent)) {
          try { screen.orientation.lock('landscape'); } catch (e) {}
        }
      } else {
        try { screen.orientation.unlock(); } catch (e) {}
      }
    });

    player.src({ src: tr ? base + '&ss=' + start : base,
                 type: info.ctype || 'video/mp4' });
    report(true);                          // 打开即记录一次起点
  }

  window.openVideoPlayer = openVideoPlayer;
  window.closeVideoPlayer = close;
})();
