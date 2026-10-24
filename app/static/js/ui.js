// Page-agnostic behaviors that markup asks for with data attributes, on every
// page (chat and admin). They stand in for inline on* handlers, which the page
// CSP doesn't allow (see CSP_PLAN.md).
(function () {
    // <input|select data-autosubmit>: submit its form on change. requestSubmit
    // fires the submit event, so an htmx form (hx-post) sends as if its submit
    // button were clicked, and a plain form navigates.
    document.addEventListener('change', function (evt) {
        const field = evt.target.closest && evt.target.closest('[data-autosubmit]');
        if (field && field.form) field.form.requestSubmit();
    });
})();
