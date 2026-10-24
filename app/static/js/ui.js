// Page-agnostic behaviors that markup asks for with data attributes, on every
// page (chat and admin). They stand in for inline on* handlers, which the page
// CSP doesn't allow (see CSP_PLAN.md).

// --- Confirmations -------------------------------------------------------------
// Destructive actions ask in the app's modal (partials/confirm_modal.html)
// instead of the browser's native dialog, which blocks the page and can't be
// styled: hx-confirm on htmx requests, data-confirm on plain forms. Without
// the modal on the page, the native dialog is the fallback.
const ConfirmDialog = (() => {
    let dialog = null;
    const setUp = () => {
        if (dialog) return dialog;
        const el = document.getElementById('confirm-modal');
        if (!el || typeof bootstrap === 'undefined') return null;
        const modal = new bootstrap.Modal(el);
        const body = document.getElementById('confirm-modal-body');
        const ok = document.getElementById('confirm-modal-ok');
        let onConfirm = null;
        ok.addEventListener('click', () => {
            const proceed = onConfirm;
            onConfirm = null;
            modal.hide();
            if (proceed) proceed();
        });
        el.addEventListener('hidden.bs.modal', () => { onConfirm = null; });
        el.addEventListener('shown.bs.modal', () => ok.focus());
        dialog = { modal, body, ok, set(fn) { onConfirm = fn; } };
        return dialog;
    };
    // The confirm button names the action when the question does.
    const labelFor = (question) => (/\bdelete\b/i.test(question) ? 'Delete'
        : /\bleave\b/i.test(question) ? 'Leave'
        : /\bremove\b/i.test(question) ? 'Remove'
        : /\bdeactivate\b/i.test(question) ? 'Deactivate' : 'Confirm');
    return {
        available: () => !!setUp(),
        labelFor,
        ask(question, proceed, label = labelFor(question)) {
            const d = setUp();
            if (!d) {
                if (window.confirm(question)) proceed();
                return;
            }
            d.body.textContent = question;
            d.ok.textContent = label;
            d.set(proceed);
            d.modal.show();
        },
    };
})();
window.ConfirmDialog = ConfirmDialog;

document.addEventListener('htmx:confirm', (evt) => {
    const question = evt.detail.question;
    if (!question || !ConfirmDialog.available()) return;
    evt.preventDefault();
    ConfirmDialog.ask(question, () => evt.detail.issueRequest(true));
});

// <form data-confirm="Remove this override?">: ask before it submits.
document.addEventListener('submit', (evt) => {
    const form = evt.target;
    if (!form.matches || !form.matches('form[data-confirm]')) return;
    if (form.dataset.confirmed) {
        delete form.dataset.confirmed;
        return;
    }
    evt.preventDefault();
    evt.stopImmediatePropagation();
    const submitter = evt.submitter;
    ConfirmDialog.ask(form.dataset.confirm, () => {
        form.dataset.confirmed = '1';
        form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
    });
}, true);

// --- Sizes from data attributes -------------------------------------------------
// The page CSP allows no style attributes, so values only known at render time
// travel as data attributes and are applied through the CSSOM (which CSP
// allows) whenever htmx loads content: <div data-width-percent="42.5">.
function applyDataSizes(root) {
    if (!root || !root.querySelectorAll) return;
    const bars = [...root.querySelectorAll('[data-width-percent]')];
    if (root.matches && root.matches('[data-width-percent]')) bars.unshift(root);
    bars.forEach((el) => {
        const percent = Math.max(0, Math.min(100, parseFloat(el.dataset.widthPercent) || 0));
        // Straight to the value: a CSS width transition would otherwise run
        // from 0 every time a bar is (re)rendered.
        el.style.transition = 'none';
        el.style.width = `${percent}%`;
        void el.offsetWidth;
        el.style.removeProperty('transition');
    });
}
if (window.htmx) htmx.onLoad(applyDataSizes);

// --- Autosubmit ------------------------------------------------------------------
// <input|select data-autosubmit>: submit its form on change. requestSubmit
// fires the submit event, so an htmx form (hx-post) sends as if its submit
// button were clicked, and a plain form navigates.
document.addEventListener('change', (evt) => {
    const field = evt.target.closest && evt.target.closest('[data-autosubmit]');
    if (field && field.form) field.form.requestSubmit();
});
