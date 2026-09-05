/* Applied before the page paints, so there is no flash of the wrong palette.
   Loaded from <head>, which is why it is its own file: the panel's CSP is
   script-src 'self', so an inline <script> in the head is not an option.

   Three states, not two. "system" is the default and follows the OS; light
   and dark are explicit overrides that stay put when the OS flips at sunset. */
(function () {
  try {
    var t = localStorage.getItem('pantheon-theme');
    if (t === 'light' || t === 'dark') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (e) { /* private window, blocked storage: system default is fine */ }
})();
