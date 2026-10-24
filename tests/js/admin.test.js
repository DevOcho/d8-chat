// The admin pages' scripts: ui.js (the in-app confirm dialog for data-confirm
// forms and hx-confirm, data-autosubmit) and admin.js (the dashboard chart,
// the escalation level target toggle, "Override this week").
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';

const ADMIN_SCRIPTS = ['htmx', 'csrf', 'ui', 'admin'];

// partials/confirm_modal.html
const CONFIRM_MODAL = `<div class="modal fade" id="confirm-modal" tabindex="-1" aria-hidden="true">
    <div class="modal-body" id="confirm-modal-body"></div>
    <button type="button" class="btn btn-light" data-bs-dismiss="modal">Cancel</button>
    <button type="button" class="btn btn-danger" id="confirm-modal-ok">Confirm</button>
</div>`;

function adminPage(content, { withModal = true, theme = 'light' } = {}) {
    return `<!doctype html><html data-bs-theme="${theme}"><head>
        <meta name="csrf-token" content="csrf-test-token">
        <meta name="htmx-config" content='{"allowEval": false, "allowScriptTags": false, "includeIndicatorStyles": false}'></head>
        <body class="admin-page"><main id="admin-content">${content}</main>
        ${withModal ? CONFIRM_MODAL : ''}</body></html>`;
}

async function adminEnv(content, opts = {}) {
    const env = await createPage({ html: adminPage(content, opts), scripts: ADMIN_SCRIPTS, xhrRoutes: opts.routes || [] });
    // Native form submissions the page let through (jsdom can't navigate).
    env.submitted = [];
    env.window.addEventListener('submit', (evt) => {
        if (!evt.defaultPrevented) env.submitted.push(evt.target.getAttribute('action'));
        evt.preventDefault();
    });
    await env.tick(10);
    return env;
}

describe('data-confirm forms', () => {
    const FORM = `<form action="/admin/oncall/schedules/1/overrides/3/delete" method="POST" data-confirm="Remove this override?">
        <button type="submit" id="remove">Remove</button></form>`;

    it('ask in the app, and submit only once confirmed', async () => {
        const env = await adminEnv(FORM);
        env.$('#remove').click();
        expect(env.$('#confirm-modal-body').textContent).toBe('Remove this override?');
        expect(env.$('#confirm-modal-ok').textContent).toBe('Remove');
        expect(env.submitted).toEqual([]);

        env.$('#confirm-modal-ok').click();
        expect(env.submitted).toEqual(['/admin/oncall/schedules/1/overrides/3/delete']);
    });

    it('ask again on the next submit', async () => {
        const env = await adminEnv(FORM);
        env.$('#remove').click();
        env.$('#confirm-modal-ok').click();
        env.$('#remove').click();
        expect(env.submitted).toHaveLength(1);
    });

    it('fall back to the native dialog when the page has no modal', async () => {
        const env = await adminEnv(FORM, { withModal: false });
        const asked = [];
        env.window.confirm = (q) => { asked.push(q); return false; };
        env.$('#remove').click();
        expect(asked).toEqual(['Remove this override?']);
        expect(env.submitted).toEqual([]);
        env.window.confirm = () => true;
        env.$('#remove').click();
        expect(env.submitted).toHaveLength(1);
    });
});

describe('hx-confirm on admin pages', () => {
    it('uses the in-app dialog too', async () => {
        const env = await adminEnv(`<button id="deactivate" hx-post="/admin/users/7/deactivate" hx-swap="none"
            hx-confirm="Deactivate robert? They lose all access immediately.">Deactivate</button>`, {
            routes: [{ match: '/admin/users/7/deactivate', reply: { body: '' } }],
        });
        env.$('#deactivate').click();
        await env.flush();
        expect(env.$('#confirm-modal-ok').textContent).toBe('Deactivate');
        expect(env.xhrs('/admin/users/7/deactivate')).toHaveLength(0);
        env.$('#confirm-modal-ok').click();
        await env.flush();
        expect(env.xhrs('/admin/users/7/deactivate').map((x) => x.method)).toEqual(['POST']);
    });
});

describe('data-autosubmit', () => {
    it('submits a plain form when its select changes (channel member role)', async () => {
        const env = await adminEnv(`<form action="/admin/channels/1/members/7/role" method="POST">
            <select name="role" id="role" data-autosubmit><option value="member">Member</option><option value="admin">Admin</option></select></form>`);
        const select = env.$('#role');
        select.value = 'admin';
        select.dispatchEvent(new env.window.Event('change', { bubbles: true }));
        expect(env.submitted).toEqual(['/admin/channels/1/members/7/role']);
    });
});

describe('dashboard chart', () => {
    const DASHBOARD = `<canvas id="messagesChart" data-chart-src="/js/local/chart.umd.min.js"></canvas>
        <script type="application/json" id="dashboard-chart-data">{"hours": ["2026-10-17T12:00:00Z", "2026-10-17T13:00:00Z"], "values": [3, 5]}</script>`;
    const fakeChart = (env) => {
        const charts = [];
        env.window.Chart = class { constructor(canvas, config) { charts.push({ canvas, config }); } };
        return charts;
    };

    it('draws from the JSON data block', async () => {
        const env = await createPage({ html: adminPage(DASHBOARD), scripts: ADMIN_SCRIPTS });
        const charts = fakeChart(env);
        await env.tick(10);
        expect(charts).toHaveLength(1);
        expect(charts[0].canvas.id).toBe('messagesChart');
        expect(charts[0].config.data.datasets[0].data).toEqual([3, 5]);
        expect(charts[0].config.data.labels.at(-1)).toBe('Now');
    });

    it('loads chart.js first when the page does not have it', async () => {
        const env = await adminEnv(DASHBOARD);
        const loader = env.$('head script[src="/js/local/chart.umd.min.js"]');
        expect(loader).not.toBeNull();
        const charts = fakeChart(env);
        loader.dispatchEvent(new env.window.Event('load'));
        expect(charts).toHaveLength(1);
    });
});

describe('escalation level target', () => {
    const ROW = `<select class="level-target-type" data-prefix="lvl-1" id="type">
            <option value="schedule" selected>Rotation</option><option value="group">Group</option><option value="user">Person</option></select>
        <select class="lvl-1-schedule" id="t-schedule"></select>
        <select class="lvl-1-group" id="t-group"></select>
        <select class="lvl-1-user" id="t-user"></select>`;

    it('shows only the input for the chosen target type', async () => {
        const env = await adminEnv(ROW);
        const shown = () => ['schedule', 'group', 'user'].filter((k) => !env.$(`#t-${k}`).hidden);
        expect(shown()).toEqual(['schedule']);
        const type = env.$('#type');
        type.value = 'user';
        type.dispatchEvent(new env.window.Event('change', { bubbles: true }));
        expect(shown()).toEqual(['user']);
    });
});

describe('"Override this week"', () => {
    it('fills the override form with the shift and focuses who covers it', async () => {
        const env = await adminEnv(`
            <button type="button" class="use-shift-dates" data-start="2026-10-19T09:00" data-end="2026-10-26T09:00">Override this week</button>
            <h5 id="override-form-title">Cover or time off</h5>
            <select id="override-user"><option value="7">robert</option></select>
            <input type="datetime-local" id="override-start"><input type="datetime-local" id="override-end">`);
        env.$('.use-shift-dates').click();
        expect(env.$('#override-start').value).toBe('2026-10-19T09:00');
        expect(env.$('#override-end').value).toBe('2026-10-26T09:00');
        expect(env.document.activeElement.id).toBe('override-user');
    });
});
