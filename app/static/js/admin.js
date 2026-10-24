// Admin pages (admin/layout.html). Each page's markup asks for what it needs
// with ids and data attributes; this sets it up on every htmx load (the page,
// then each swap into #admin-content). Shared behaviors (data-confirm,
// data-autosubmit) are in ui.js.

// Every element matching `selector` in a newly loaded root, the root included.
function adminWithin(root, selector) {
    if (!root || !root.querySelectorAll) return [];
    const found = [...root.querySelectorAll(selector)];
    if (root.matches && root.matches(selector)) found.unshift(root);
    return found;
}

// --- Dashboard: messages per hour (admin/dashboard_content.html) ---
const DashboardChart = {
    draw(canvas, data) {
        const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
        const gridColor = dark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)';
        const textColor = dark ? '#dee2e6' : '#6c757d';
        // Hour starts come as UTC ISO strings; label them in the viewer's own
        // time zone, the current hour as "Now".
        const hours = data.hours || [];
        const labels = hours.map((iso, i) => (i === hours.length - 1
            ? 'Now'
            : new Date(iso).toLocaleTimeString([], { hour: 'numeric' })));
        new Chart(canvas, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: 'Messages',
                    data: data.values || [],
                    backgroundColor: 'rgba(54, 185, 204, 0.5)',
                    borderColor: 'rgb(54, 185, 204)',
                    borderWidth: 1,
                }],
            },
            options: {
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0, color: textColor }, grid: { color: gridColor } },
                    x: { ticks: { color: textColor }, grid: { color: gridColor } },
                },
                plugins: { legend: { display: false } },
            },
        });
    },
    init(canvas) {
        const source = document.getElementById('dashboard-chart-data');
        if (!source || canvas.dataset.drawn) return;
        canvas.dataset.drawn = '1';
        let data;
        try {
            data = JSON.parse(source.textContent);
        } catch (e) {
            console.error('Dashboard chart data is not valid JSON.', e);
            return;
        }
        // chart.js loads on demand: only the dashboard needs it.
        if (window.Chart) {
            DashboardChart.draw(canvas, data);
            return;
        }
        const script = document.createElement('script');
        script.src = canvas.dataset.chartSrc;
        script.onload = () => DashboardChart.draw(canvas, data);
        document.head.appendChild(script);
    },
};

// --- On-call escalation policy (admin/oncall_policy.html) ---
// Each level row shows only the target input for its target type
// (<select class="level-target-type" data-prefix="..."> drives the
// .<prefix>-schedule / -group / -user inputs).
function syncLevelTarget(select) {
    const prefix = select.dataset.prefix;
    ['schedule', 'group', 'user'].forEach((kind) => {
        document.querySelectorAll(`.${prefix}-${kind}`).forEach((el) => {
            el.hidden = select.value !== kind;
        });
    });
}
document.addEventListener('change', (evt) => {
    if (evt.target.matches && evt.target.matches('.level-target-type')) syncLevelTarget(evt.target);
});

// --- On-call rotation (admin/oncall_schedule.html) ---
// "Override this week" fills the override form with that shift's bounds.
document.addEventListener('click', (evt) => {
    const btn = evt.target.closest && evt.target.closest('.use-shift-dates');
    if (!btn) return;
    document.getElementById('override-start').value = btn.dataset.start;
    document.getElementById('override-end').value = btn.dataset.end;
    document.getElementById('override-form-title').scrollIntoView({ behavior: 'smooth' });
    document.getElementById('override-user').focus();
});

htmx.onLoad((root) => {
    adminWithin(root, '#messagesChart').forEach((canvas) => DashboardChart.init(canvas));
    adminWithin(root, '.level-target-type').forEach(syncLevelTarget);
});
