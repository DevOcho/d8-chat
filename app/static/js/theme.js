// Resolve the "system" theme to light or dark, and follow the OS setting.
// Loaded in <head>, before the body renders, so the page never flashes the
// wrong theme. (A file rather than an inline <script>; see CSP_PLAN.md.)
(function () {
    const htmlElement = document.documentElement;
    if (htmlElement.getAttribute('data-bs-theme') !== 'system') return;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = (dark) => htmlElement.setAttribute('data-bs-theme', dark ? 'dark' : 'light');
    apply(query.matches);
    query.addEventListener('change', (e) => apply(e.matches));
})();
