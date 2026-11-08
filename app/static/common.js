/* ============================================================
   公共 JS 工具: 所有页面共享 (须在页面自身脚本之前引入)
   注意: 这里的顶层 const/let 与页面脚本共用全局词法作用域,
   页面内不得重复声明同名标识符 ($ esc escH fmtSize toast ME hasPerm loadMe api)
   ============================================================ */
const $ = id => document.getElementById(id);

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const escH = esc;                       // 控制页历史别名

const fmtSize = b => {
  if (b == null) return '-';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + ' ' + u[i];
};

/* 轻提示: 页面需有 <div id="toast"> 元素, 没有则静默忽略 */
function toast(msg, ms) {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), ms || 2200);
}

/* ---------- 当前账号与权限 ---------- */
let ME = { perms: [], is_admin: false };
const hasPerm = f => ME.is_admin || (ME.perms || []).includes(f);

/* 拉取 /api/me: 成功返回 true; 401 自动跳登录页; 网络异常返回 false (由调用方决定降级) */
async function loadMe() {
  try {
    const r = await fetch('/api/me');
    if (r.status === 401) { location.href = '/login'; return false; }
    ME = await r.json();
    return true;
  } catch (e) { return false; }
}

/* 带登录跳转与错误透出的 fetch JSON (X-Client-Net 供服务端识别线路) */
async function api(url, opt) {
  opt = Object.assign({}, opt);
  opt.headers = Object.assign({}, opt.headers,
    { 'X-Client-Net': (window.NetCheck && NetCheck.current()) || 'unknown' });
  const r = await fetch(url, opt);
  if (r.status === 401) { location.href = '/login'; throw new Error('unauth'); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
