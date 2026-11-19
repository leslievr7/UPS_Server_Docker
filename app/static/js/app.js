/*
 * Small shared behaviours for every page.
 *
 * Theme: "system" (follow the device), "light" or "dark", remembered per
 * browser. The <head> applies it before first paint to avoid a white flash;
 * this file keeps it in sync with the menu and with the device setting.
 */
(function () {
  'use strict';

  var KEY = 'upsThemeMode';
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  function stored() {
    try { return localStorage.getItem(KEY) || 'system'; } catch (e) { return 'system'; }
  }

  function apply(mode) {
    var dark = mode === 'dark' || (mode === 'system' && media.matches);
    var root = document.documentElement;
    root.classList.toggle('dark', dark);
    root.dataset.themeMode = mode;
    document.querySelectorAll('[data-theme-option]').forEach(function (el) {
      el.setAttribute('aria-checked', String(el.dataset.themeOption === mode));
    });
    document.querySelectorAll('[data-theme-icon]').forEach(function (el) {
      el.hidden = el.dataset.themeIcon !== (mode === 'system' ? 'system' : (dark ? 'dark' : 'light'));
    });
  }

  window.setThemeMode = function (mode) {
    try { localStorage.setItem(KEY, mode); } catch (e) { /* private mode */ }
    apply(mode);
  };

  media.addEventListener('change', function () {
    if (stored() === 'system') apply('system');
  });

  document.addEventListener('DOMContentLoaded', function () {
    apply(stored());
    document.querySelectorAll('[data-theme-option]').forEach(function (el) {
      el.addEventListener('click', function () { window.setThemeMode(el.dataset.themeOption); });
    });
  });
})();
