/* 主题切换: 在 <head> 中尽早加载, 避免首屏闪烁 */
(function () {
  var theme = localStorage.getItem('theme') || 'light';
  document.documentElement.dataset.theme = theme;
  window.toggleTheme = function () {
    theme = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('theme', theme);
    document.dispatchEvent(new CustomEvent('themechange'));
  };
})();
