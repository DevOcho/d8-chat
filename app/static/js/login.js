// Login page: show / hide the password. (A file rather than an inline
// <script>; see CSP_PLAN.md.)
document.addEventListener('DOMContentLoaded', function () {
    const toggle = document.getElementById('toggle-password-visibility');
    const passwordInput = document.getElementById('password');
    if (!toggle || !passwordInput) return;
    const icon = toggle.querySelector('i');
    toggle.addEventListener('click', function () {
        const show = passwordInput.getAttribute('type') === 'password';
        passwordInput.setAttribute('type', show ? 'text' : 'password');
        if (icon) {
            icon.classList.toggle('bi-eye', show);
            icon.classList.toggle('bi-eye-slash', !show);
        }
    });
});
