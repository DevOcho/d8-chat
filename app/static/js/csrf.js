// Attach the CSRF token to every state-changing HTMX request.
// (A file rather than an inline <script>, so the page CSP can be
// script-src 'self'; see CSP_PLAN.md.)
(function () {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!token) return;
    document.body.addEventListener('htmx:configRequest', function (evt) {
        const method = (evt.detail.verb || '').toLowerCase();
        if (method && method !== 'get' && method !== 'head') {
            evt.detail.headers['X-CSRFToken'] = token;
        }
    });
})();
