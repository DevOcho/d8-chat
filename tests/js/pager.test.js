// app/static/js/pager.js — the on-call pager overlay, siren, title flash,
// desktop notification, acknowledge flow, and the active-pages sync (on load,
// on every socket (re)open, every 30s while ringing, and on tab refocus).
//
// Frames reach the pager the way they do in production: through the fake
// socket, htmx-ws and chat.js's htmx:wsAfterMessage routing.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import { deferred } from './helpers/fakes.js';
import {
    ME, chatPage, conversationPane, incident, message, pageClearedFrame, pageFrame, testPageFrame,
} from './helpers/fixtures.js';

const RESYNC_MS = 30000;

/**
 * @param {object} o
 * @param {Array}  [o.active]   incidents GET /oncall/pages/active returns
 * @param {object} [o.page]     chatPage() overrides
 * @param {Array}  [o.fetchRoutes]  extra routes, matched before the
 *                 /oncall/pages/active stub below (so they can override it)
 *
 * env.server.active is what the next sync returns; set env.server.gate to a
 * deferred() to hold the next sync's response until the test resolves it
 * with a reply spec ({ json }).
 */
async function pagerEnv({ active = [], page = {}, fetchRoutes = [], ...opts } = {}) {
    const server = { active, gate: null };
    const env = await createPage({
        html: chatPage({ pane: conversationPane({ convIdStr: 'channel_5' }), ...page }),
        ...opts,
        fetchRoutes: [
            ...fetchRoutes,
            {
                match: '/oncall/pages/active',
                reply: async () => {
                    if (server.gate) {
                        const gate = server.gate;
                        server.gate = null;
                        return gate.promise;
                    }
                    return { json: { incidents: server.active } };
                },
            },
        ],
    });
    env.server = server;
    return env;
}

const overlay = (env) => env.$('#pager-overlay');
const rows = (env) => env.$$('#pager-incidents .pager-incident');
const syncs = (env) => env.fetches('/oncall/pages/active').length;
const sirenOn = (env) => env.state.audioContexts.some((ctx) => ctx.oscillators.some((o) => o.started && !o.stopped));
const ringing = (env) => !overlay(env).hidden && env.document.body.classList.contains('pager-active') && sirenOn(env);
const localTime = (env, iso) => env.eval(`new Date('${iso}Z').toLocaleString([], { hour: '2-digit', minute: '2-digit' })`);

describe('initial active-pages sync', () => {
    it('asks the server what is ringing on load', async () => {
        const env = await pagerEnv();
        const [call] = env.fetches('/oncall/pages/active');
        expect(call.init).toMatchObject({ credentials: 'same-origin', headers: { Accept: 'application/json' } });
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('rings for an incident that was already paging before the page loaded', async () => {
        const env = await pagerEnv({ active: [incident()] });
        expect(ringing(env)).toBe(true);
        expect(rows(env).map((r) => r.dataset.incidentId)).toEqual(['42']);
    });

    it('survives a failing sync endpoint', async () => {
        const env = await pagerEnv({
            fetchRoutes: [{ match: '/oncall/pages/active', reply: { status: 500, json: {} } }],
        });
        expect(overlay(env).hidden).toBe(true);
        env.fetch.mockRejectedValueOnce(new TypeError('Failed to fetch'));
        await env.openSocket(); // wsOpen → sync, which now rejects
        expect(overlay(env).hidden).toBe(true);
        expect(env.state.console.some((c) => c.level === 'warn' && String(c.args[0]).includes('Pager sync failed'))).toBe(true);
    });
});

describe('page frame', () => {
    it('shows the overlay with the incident and starts ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));

        expect(ringing(env)).toBe(true);
        const [row] = rows(env);
        expect(row.querySelector('.pager-incident-title').textContent).toBe('🔴 DOWN: api.devocho.com');
        const metas = [...row.querySelectorAll('.pager-incident-meta')].map((m) => m.textContent);
        // Naive-UTC timestamps are read as UTC and shown in local time.
        expect(localTime(env, '2026-10-17T13:58:00')).toMatch(/09:58/);
        expect(metas).toEqual([
            `https://api.devocho.com/health · #devops · since ${localTime(env, '2026-10-17T13:58:00')}`,
            `Paging: Kenny · escalates at ${localTime(env, '2026-10-17T14:03:00')}`,
        ]);
        expect(row.querySelector('.pager-incident-details').textContent).toBe('Connection refused');
        expect(row.querySelector('[data-pager-ack]').textContent).toBe('Acknowledge');
        expect(row.querySelector('[data-pager-view]').dataset.pagerView).toBe('900');
    });

    it('builds the siren from two square-wave oscillators (tone + LFO)', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        const [ctx] = env.state.audioContexts;
        expect(ctx.oscillators).toHaveLength(2);
        expect(ctx.oscillators.every((o) => o.type === 'square' && o.started)).toBe(true);
        expect(ctx.oscillators.map((o) => o.frequency.value)).toEqual([865, 1.6]);
    });

    it('flashes the tab title every second', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.tick(1000);
        expect(env.document.title).toBe('🚨 PAGE: api.devocho.com');
        await env.tick(1000);
        expect(env.document.title).toBe(env.eval('TitleManager.current()'));
        await env.tick(1000);
        expect(env.document.title).toBe('🚨 PAGE: api.devocho.com');
    });

    it('raises a sticky desktop notification using the page favicon', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(env.notifications).toHaveLength(1);
        const [n] = env.notifications;
        expect(n.title).toBe('🚨 DOWN: api.devocho.com');
        expect(n.options).toEqual({
            body: 'Connection refused',
            tag: 'incident-42',
            requireInteraction: true,
            renotify: false,
            icon: 'https://d8-chat.local/favicon-32x32.png?v=0123abcd',
        });
    });

    // The app serves static files at the root (Flask static_url_path=""), so
    // the fallback must be /favicon-32x32.png, not /static/....
    it('falls back to a favicon URL the app actually serves', async () => {
        const env = await pagerEnv();
        env.$$('link[rel="icon"]').forEach((l) => l.remove());
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(env.notifications[0].options.icon).toBe('/favicon-32x32.png');
    });

    it('re-notifies (renotify: true) for the server\'s repeat of a page, without a duplicate row', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.frame(pageFrame(incident({ current_level: 2 }), { repeat: true }));
        expect(rows(env)).toHaveLength(1);
        expect(env.notifications.map((n) => n.options.renotify)).toEqual([false, true]);
    });

    it('does not notify when notification permission was not granted', async () => {
        const env = await pagerEnv({ notificationPermission: 'denied' });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(ringing(env)).toBe(true);
        expect(env.notifications).toEqual([]);
    });

    it('focuses the window and unlocks audio when the notification is clicked', async () => {
        const env = await pagerEnv({ autoplayBlocked: true });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(env.state.audioContexts[0].state).toBe('suspended');
        let focused = false;
        env.window.focus = () => { focused = true; };
        env.state.autoplayBlocked = false; // clicking the notification is a user gesture
        env.notifications[0].onclick();
        await env.flush();
        expect(focused).toBe(true);
        expect(env.state.audioContexts[0].state).toBe('running');
    });
});

describe('page_cleared frame', () => {
    it('dismisses the overlay and stops the siren, title flash and notification', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.tick(1000);
        await env.frame(pageClearedFrame(incident()));

        expect(overlay(env).hidden).toBe(true);
        expect(env.document.body.classList.contains('pager-active')).toBe(false);
        expect(sirenOn(env)).toBe(false);
        const [ctx] = env.state.audioContexts;
        expect(ctx.oscillators.every((o) => o.stopped && o.disconnect.mock.calls.length === 1)).toBe(true);
        expect(env.document.title).toBe(env.eval('TitleManager.current()'));
        expect(env.notifications[0].closed).toBe(true);
        await env.tick(5000);
        expect(env.document.title).toBe(env.eval('TitleManager.current()'));
    });

    it('ignores a page_cleared for an incident that is not ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.frame(pageClearedFrame(incident({ id: 7 })));
        expect(ringing(env)).toBe(true);
    });
});

describe('acknowledge', () => {
    async function ringingEnv(ackReply) {
        const env = await pagerEnv({ fetchRoutes: [{ match: '/oncall/incidents/42/ack', reply: ackReply }] });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        return env;
    }

    it('POSTs to /oncall/incidents/<id>/ack with the CSRF token, then stops ringing', async () => {
        const gate = deferred();
        const env = await ringingEnv(() => gate.promise);
        const button = env.$('[data-pager-ack="42"]');
        button.click();
        await env.flush();
        expect(button.disabled).toBe(true);
        const [call] = env.fetches('/oncall/incidents/42/ack');
        expect(call.init).toMatchObject({
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': 'csrf-test-token', Accept: 'application/json' },
        });
        expect(ringing(env)).toBe(true);

        gate.resolve({ json: { ok: true } });
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('shows the server error, re-enables the button and keeps ringing when the ack fails', async () => {
        const env = await ringingEnv({ status: 403, json: { error: 'You are not paged for this incident' } });
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        const error = env.$('[data-incident-id="42"] .pager-incident-error');
        expect(error.hidden).toBe(false);
        expect(error.textContent).toBe("Couldn't acknowledge: You are not paged for this incident. Try again.");
        expect(env.$('[data-pager-ack="42"]').disabled).toBe(false);
        expect(ringing(env)).toBe(true);
    });

    it('reports the HTTP status when the error response is not JSON', async () => {
        const env = await ringingEnv({ status: 502 });
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        expect(env.$('[data-incident-id="42"] .pager-incident-error').textContent)
            .toBe("Couldn't acknowledge: HTTP 502. Try again.");
    });

    it('reports a network failure', async () => {
        const env = await ringingEnv(new TypeError('Failed to fetch'));
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        expect(env.$('[data-incident-id="42"] .pager-incident-error').textContent)
            .toBe("Couldn't acknowledge: Failed to fetch. Try again.");
        expect(ringing(env)).toBe(true);
    });
});

describe('multiple concurrent incidents', () => {
    it('lists every ringing incident and keeps ringing until the last one clears', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.frame(pageFrame(incident({ id: 43, title: 'db.devocho.com', message_id: null })));
        expect(rows(env).map((r) => r.dataset.incidentId)).toEqual(['42', '43']);
        expect(rows(env)[1].querySelector('[data-pager-view]')).toBeNull();
        expect(env.notifications.map((n) => n.options.tag)).toEqual(['incident-42', 'incident-43']);
        // One siren for all of them.
        expect(env.state.audioContexts[0].oscillators).toHaveLength(2);

        await env.frame(pageClearedFrame(incident()));
        expect(rows(env).map((r) => r.dataset.incidentId)).toEqual(['43']);
        expect(ringing(env)).toBe(true);
        expect(env.notifications[0].closed).toBe(true);
        expect(env.notifications[1].closed).toBe(false);
        await env.tick(1000);
        expect(env.document.title).toBe('🚨 PAGE: db.devocho.com');

        await env.frame(pageClearedFrame(incident({ id: 43 })));
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });
});

describe('test pages', () => {
    it('renders a test page and stops it locally without calling the server', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(testPageFrame('test-123'));
        const [row] = rows(env);
        expect(row.querySelector('.pager-incident-title').textContent).toBe('🧪 Test page');
        expect(row.textContent).not.toContain('Paging:');
        const stop = row.querySelector('[data-pager-ack]');
        expect(stop.textContent).toBe('Stop test');
        expect(ringing(env)).toBe(true);

        stop.click();
        await env.flush();
        expect(env.fetches(/\/ack$/)).toEqual([]);
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('survives a sync that does not list it', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(testPageFrame('test-123'));
        const before = syncs(env);
        await env.tick(RESYNC_MS);
        expect(syncs(env)).toBe(before + 1);
        expect(rows(env).map((r) => r.dataset.incidentId)).toEqual(['test-123']);
        expect(ringing(env)).toBe(true);
    });
});

describe('periodic re-sync while ringing', () => {
    it('re-checks the server every 30s while ringing and stops after the last clear', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = [incident()];
        const base = syncs(env);
        await env.tick(RESYNC_MS - 1);
        expect(syncs(env)).toBe(base);
        await env.tick(1);
        expect(syncs(env)).toBe(base + 1);
        await env.tick(RESYNC_MS);
        expect(syncs(env)).toBe(base + 2);

        await env.frame(pageClearedFrame(incident()));
        await env.tick(RESYNC_MS * 4);
        expect(syncs(env)).toBe(base + 2);
    });

    it('does not poll when nothing is ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        const base = syncs(env);
        await env.tick(RESYNC_MS * 4);
        expect(syncs(env)).toBe(base);
    });

    it('silences an incident whose page_cleared frame was lost', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = []; // acknowledged elsewhere; the frame never arrived
        await env.tick(RESYNC_MS);
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('does not re-notify for an incident that is already ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = [incident()];
        await env.tick(RESYNC_MS * 3);
        expect(env.notifications).toHaveLength(1);
        expect(rows(env)).toHaveLength(1);
    });

    it('picks up a new incident it missed', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = [incident(), incident({ id: 43, title: 'db.devocho.com' })];
        await env.tick(RESYNC_MS);
        expect(rows(env).map((r) => r.dataset.incidentId)).toEqual(['42', '43']);
        expect(env.notifications.map((n) => n.options.tag)).toEqual(['incident-42', 'incident-43']);
    });

    it('does not bring back an incident cleared while a sync was in flight (stale response)', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        const gate = deferred();
        env.server.gate = gate;
        await env.tick(1000);
        await env.tick(RESYNC_MS - 1000); // the resync fires and waits on `gate`
        await env.tick(250);
        await env.frame(pageClearedFrame(incident())); // acknowledged meanwhile
        expect(overlay(env).hidden).toBe(true);

        gate.resolve({ json: { incidents: [incident()] } }); // answer from before the ack
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
        expect(env.notifications).toHaveLength(1);
    });

    it('rings again for an incident the server still lists after a later sync', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        await env.frame(pageClearedFrame(incident()));
        // Only a sync that started before the clear counts as stale; one
        // started afterwards is trusted.
        await env.tick(10);
        env.server.active = [incident()];
        env.trigger(env.document.body, 'htmx:wsOpen');
        await env.flush();
        expect(ringing(env)).toBe(true);
    });

    // A sync's answer predates any page that arrives while it's in flight, so
    // it must not clear one (that would silence a new page for up to a minute).
    it('does not silence a page that arrived while a sync was in flight', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        const gate = deferred();
        env.server.gate = gate;
        env.trigger(env.document.body, 'htmx:wsOpen'); // reconnect → sync starts
        await env.flush();
        await env.tick(100);
        await env.frame(pageFrame(incident())); // new page arrives meanwhile
        expect(ringing(env)).toBe(true);

        gate.resolve({ json: { incidents: [] } }); // snapshot from before the page
        await env.flush();
        expect(ringing(env)).toBe(true);
    });
});

describe('sync triggers', () => {
    it('syncs on every socket (re)open', async () => {
        const env = await pagerEnv();
        const base = syncs(env);
        await env.openSocket();
        expect(syncs(env)).toBe(base + 1);
        env.socket.serverClose(1006);
        env.window.Math.random = () => 0;
        await env.tick(10);
        await env.openSocket();
        expect(syncs(env)).toBe(base + 2);
    });

    it('syncs when the tab becomes visible while something is ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = [incident()];
        await env.setVisible(false);
        const base = syncs(env);
        await env.setVisible(true);
        expect(syncs(env)).toBe(base + 1);
    });

    it('does not sync on visibility changes when nothing is ringing', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        const base = syncs(env);
        await env.setVisible(false);
        await env.setVisible(true);
        expect(syncs(env)).toBe(base);
    });

    it('does not sync when the tab is hidden', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.server.active = [incident()];
        const base = syncs(env);
        await env.setVisible(false);
        expect(syncs(env)).toBe(base);
    });
});

describe('audio unlock', () => {
    it('shows the "click to start the alarm" hint while autoplay is blocked, until a click', async () => {
        const env = await pagerEnv({ autoplayBlocked: true });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        const hint = env.$('#pager-sound-hint');
        expect(sirenOn(env)).toBe(true); // started, but on a suspended context
        expect(hint.hidden).toBe(false);
        env.state.autoplayBlocked = false; // the click is a user gesture
        env.document.body.click();
        await env.flush();
        expect(env.state.audioContexts[0].state).toBe('running');
        expect(hint.hidden).toBe(true);
    });

    it('keeps the hint hidden when audio is allowed', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(env.$('#pager-sound-hint').hidden).toBe(true);
    });
});

describe('view in channel', () => {
    it('scrolls to the alert message when it is on screen', async () => {
        const env = await pagerEnv({
            page: {
                pane: conversationPane({
                    convIdStr: 'channel_5',
                    rows: message({ id: 900, user: ME, createdAt: '2026-10-17T13:58:00', content: 'DOWN' }),
                }),
            },
        });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        const scrolled = [];
        env.window.Element.prototype.scrollIntoView = function scrollIntoView() { scrolled.push(this.id); };
        env.$('[data-pager-view="900"]').click();
        expect(scrolled).toEqual(['message-900']);
    });

    it('loads the message in context when it is not on screen', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        env.$('[data-pager-view="900"]').click();
        await env.flush();
        expect(env.xhrs('/chat/message/900/context')).toHaveLength(1);
    });
});

describe('incident timestamps', () => {
    it('localizes the "on call until" badge on load', async () => {
        const env = await pagerEnv({ page: { oncallUntil: '2026-10-19T13:00:00' } });
        const el = env.$('.oncall-badge .incident-time');
        const expected = env.eval("new Date('2026-10-19T13:00:00Z').toLocaleString([], { hour: '2-digit', minute: '2-digit', weekday: 'short' })");
        expect(el.textContent).toBe(expected);
        // data-format="datetime" adds the weekday.
        expect(expected).not.toBe(localTime(env, '2026-10-19T13:00:00'));
        expect(el.classList.contains('time-processed')).toBe(true);
    });

    it('localizes an incident card swapped in over the socket', async () => {
        const env = await pagerEnv({
            page: {
                pane: conversationPane({
                    convIdStr: 'channel_5',
                    rows: '<div id="incident-card-42" class="incident-card incident-triggered mt-2"></div>',
                }),
            },
        });
        await env.openSocket();
        await env.frame(`<div id="incident-card-42" class="incident-card incident-acknowledged mt-2" hx-swap-oob="outerHTML">
            Acknowledged by <strong>Kenny</strong> at
            <span class="incident-time" data-timestamp="2026-10-17T14:05:00Z">14:05 UTC</span>
        </div>`);
        await env.tick(0);
        expect(env.$('#incident-card-42 .incident-time').textContent).toBe(localTime(env, '2026-10-17T14:05:00'));
    });
});
