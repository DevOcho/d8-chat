// app/static/js/pager.js — the on-call pager overlay, siren, title flash,
// desktop notification, acknowledge flow, and the active-pages sync (on load,
// on every socket (re)open, every 30s while ringing, and on tab refocus).
// Also D8's lead pages: Accept and Reject, the green screen, and the recorded
// ka-ching with its synthesized fallback.
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
const KACHING_TOPUP_MS = 15000;

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

    it('POSTs to /oncall/incidents/<id>/ack with the CSRF token, then clears the page', async () => {
        const gate = deferred();
        const env = await ringingEnv(() => gate.promise);
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        expect(env.$('[data-pager-ack="42"]').disabled).toBe(true);
        const [call] = env.fetches('/oncall/incidents/42/ack');
        expect(call.init).toMatchObject({
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': 'csrf-test-token', Accept: 'application/json' },
        });
        // Silenced before the server answers, still on screen.
        expect(sirenOn(env)).toBe(false);
        expect(overlay(env).hidden).toBe(false);
        expect(env.$('.pager-incident-silenced')).toBeNull();

        gate.resolve({ json: { ok: true } });
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('shows the server error, re-enables the button and keeps the page up, silenced, when the ack fails', async () => {
        const env = await ringingEnv({ status: 403, json: { error: 'You are not paged for this incident' } });
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        const error = env.$('[data-incident-id="42"] .pager-incident-error');
        expect(error.hidden).toBe(false);
        expect(error.textContent).toBe("Couldn't acknowledge: You are not paged for this incident. Try again.");
        expect(env.$('[data-pager-ack="42"]').disabled).toBe(false);
        expect(overlay(env).hidden).toBe(false);
        expect(sirenOn(env)).toBe(false);
        expect(env.$('[data-incident-id="42"] .pager-incident-silenced').textContent)
            .toBe('🔇 Silenced in this tab. The page keeps escalating until someone acknowledges.');
    });

    it('reports the HTTP status when the error response is not JSON', async () => {
        const env = await ringingEnv({ status: 502 });
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        expect(env.$('[data-incident-id="42"] .pager-incident-error').textContent)
            .toBe("Couldn't acknowledge: HTTP 502. Try again.");
    });

    it('offline: reports the failure and stays silenced', async () => {
        const env = await ringingEnv(new TypeError('Failed to fetch'));
        env.$('[data-pager-ack="42"]').click();
        await env.flush();
        expect(env.$('[data-incident-id="42"] .pager-incident-error').textContent)
            .toBe("Couldn't acknowledge: Failed to fetch. Try again.");
        expect(sirenOn(env)).toBe(false);
        expect(env.$('[data-incident-id="42"] .pager-incident-silenced')).not.toBeNull();
    });

    it('the server re-sending a silenced page rings it again', async () => {
        const env = await ringingEnv(new TypeError('Failed to fetch'));
        env.$('[data-pager-ack="42"]').click();
        await env.flush();

        await env.frame(pageFrame(incident(), { repeat: true }));

        expect(ringing(env)).toBe(true);
        expect(env.$('.pager-incident-silenced')).toBeNull();
        expect(env.$('[data-incident-id="42"] .pager-incident-error').hidden).toBe(true);
    });

    it('a re-send while the ack is out stays silent and keeps the button disabled', async () => {
        const gate = deferred();
        const env = await ringingEnv(() => gate.promise);
        env.$('[data-pager-ack="42"]').click();
        await env.flush();

        await env.frame(pageFrame(incident(), { repeat: true }));

        expect(sirenOn(env)).toBe(false);
        expect(env.$('[data-pager-ack="42"]').disabled).toBe(true);
        gate.resolve({ json: { ok: true } });
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
    });

    it('silencing one page leaves another ringing', async () => {
        const env = await ringingEnv(new TypeError('Failed to fetch'));
        await env.frame(pageFrame(incident({ id: 43, title: 'Other' })));
        env.$('[data-pager-ack="42"]').click();
        await env.flush();

        expect(sirenOn(env)).toBe(true);
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

describe('ka-ching (website leads from D8)', () => {
    const BELL_HZ = 2093;
    const lead = (overrides = {}) => incident({
        id: 77,
        title: 'New website lead: Dana Whitfield at Meridian Health Systems',
        details: 'Filled in the Contact page form. Sam SDR has it.',
        source: 'd8',
        source_url: 'https://new-d8.local/marketing/leads/12',
        alarm: 'ka-ching',
        channel_name: 'website-leads',
        message_id: 901,
        ...overrides,
    });
    const ctxOf = (env) => env.state.audioContexts[0];
    // When each scheduled ka-ching plays, from its bell's start (struck 0.09 s
    // after the "ka"), relative to when the alarm started (`from`, plus the
    // 0.05 s the first one is held back).
    const kachings = (env, from = 0, sinceIndex = 0) => ctxOf(env).oscillators.slice(sinceIndex)
        .filter((o) => o.frequency.value === BELL_HZ)
        .map((o) => Math.round((o.startAt - 0.09 - 0.05 - from) * 1000) / 1000);
    const masters = (env) => ctxOf(env).gains.filter((g) => g.gain.value === 0.5
        && g.connect.mock.calls.some(([dest]) => dest === ctxOf(env).destination));
    const liveMasters = (env) => masters(env).filter((g) => g.disconnect.mock.calls.length === 0);
    const ringingLead = async (inc = lead()) => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(inc));
        return env;
    };

    it('plays every 6 s on the audio clock and never slows down', async () => {
        const env = await ringingLead();

        // Scheduled 180 s ahead: the one due at 180 s goes on the next top-up.
        const every6 = Array.from({ length: 30 }, (_, n) => n * 6);
        expect(kachings(env)).toEqual(every6);
        expect(sirenOn(env)).toBe(false);
        expect(ctxOf(env).oscillators.some((o) => o.type === 'square')).toBe(false);
        expect(ctxOf(env).bufferSources.map((s) => Math.round((s.startAt - 0.05) * 1000) / 1000))
            .toEqual(every6);
        expect(env.eval('[0, 10, 11, 100].map(PagerManager.kachingOffset)')).toEqual([0, 60, 66, 600]);
    });

    it('tops the schedule up as the audio clock runs, without repeating one', async () => {
        const env = await ringingLead();
        ctxOf(env).currentTime = 100;
        await env.tick(15000);

        const times = kachings(env);
        expect(times.at(-1)).toBe(276);
        expect(new Set(times).size).toBe(times.length);
        expect(times.every((t, i) => t === i * 6)).toBe(true);
    });

    it('skips the ka-chings a sleeping tab missed instead of playing them all at once', async () => {
        const env = await ringingLead();
        ctxOf(env).currentTime = 500;
        await env.tick(15000);

        const late = kachings(env).filter((t) => t > 180);
        expect(late.length).toBeGreaterThan(0);
        expect(late.every((t) => t >= 500 - 0.05)).toBe(true);
    });

    it('a new lead starts the ka-ching again; a repeat of the same one does not', async () => {
        const env = await ringingLead();
        ctxOf(env).currentTime = 120;
        await env.frame(pageFrame(lead(), { repeat: true }));
        expect(liveMasters(env)).toHaveLength(1);
        expect(masters(env)).toHaveLength(1);

        const before = ctxOf(env).oscillators.length;
        await env.frame(pageFrame(lead({ id: 78, title: 'New website lead: Luis Ortega' })));

        expect(masters(env)).toHaveLength(2);
        expect(liveMasters(env)).toHaveLength(1);
        expect(kachings(env, 120, before).slice(0, 4)).toEqual([0, 6, 12, 18]);
    });

    it('stops when the page is cleared, including the ka-chings already scheduled', async () => {
        const env = await ringingLead();
        await env.frame(pageClearedFrame(lead()));

        expect(overlay(env).hidden).toBe(true);
        expect(liveMasters(env)).toHaveLength(0);
        const sources = [...ctxOf(env).oscillators, ...ctxOf(env).bufferSources];
        expect(sources.every((s) => s.stop.mock.calls.length >= 1)).toBe(true);
        const scheduled = kachings(env).length;
        ctxOf(env).currentTime = 200;
        await env.tick(KACHING_TOPUP_MS * 2);
        expect(kachings(env)).toHaveLength(scheduled);
    });

    it('Acknowledge silences it at once, offline too, until the server re-sends it', async () => {
        const env = await pagerEnv({
            fetchRoutes: [{ match: '/oncall/incidents/77/ack', reply: new TypeError('Failed to fetch') }],
        });
        await env.openSocket();
        await env.frame(pageFrame(lead()));
        expect(liveMasters(env)).toHaveLength(1);

        env.$('[data-pager-ack="77"]').click();
        await env.flush();

        expect(overlay(env).hidden).toBe(false);
        expect(liveMasters(env)).toHaveLength(0);
        expect(env.$('[data-incident-id="77"] .pager-incident-silenced')).not.toBeNull();

        await env.frame(pageFrame(lead(), { repeat: true }));
        expect(liveMasters(env)).toHaveLength(1);
    });

    it('reads as a lead and links to it in D8', async () => {
        const env = await ringingLead();
        const [row] = rows(env);

        expect(row.querySelector('.pager-incident-title').textContent)
            .toBe('💰 New website lead: Dana Whitfield at Meridian Health Systems');
        const metas = [...row.querySelectorAll('.pager-incident-meta')].map((m) => m.textContent);
        expect(metas[0]).toBe(`#website-leads · since ${localTime(env, '2026-10-17T13:58:00')}`);
        const open = row.querySelector('a.btn');
        expect(open.textContent).toBe('Open the lead in D8');
        expect(open.href).toBe('https://new-d8.local/marketing/leads/12');
        expect(open.target).toBe('_blank');
        expect(open.rel).toBe('noopener noreferrer');
        expect(env.notifications[0].title).toBe('💰 New website lead: Dana Whitfield at Meridian Health Systems');
        await env.tick(1000);
        expect(env.document.title).toBe('💰 LEAD: New website lead: Dana Whitfield at Meridian Health Systems');
    });

    it('does not link a URL that is not http(s)', async () => {
        const env = await ringingLead(lead({ source_url: 'javascript:alert(1)' }));
        expect(rows(env)[0].querySelector('a.btn')).toBeNull();
    });

    it('plays both alarms for a lead and an outage, and keeps the ka-ching when the outage clears', async () => {
        const env = await ringingLead();
        await env.frame(pageFrame(incident()));
        expect(sirenOn(env)).toBe(true);
        expect(liveMasters(env)).toHaveLength(1);

        await env.frame(pageClearedFrame(incident()));
        expect(sirenOn(env)).toBe(false);
        expect(liveMasters(env)).toHaveLength(1);
        expect(overlay(env).hidden).toBe(false);
    });

    it('plays the ka-ching for a test page that asks for it', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        const frame = testPageFrame('test-1792245600-ka-ching');
        frame.incident.alarm = 'ka-ching';
        await env.frame(frame);

        expect(kachings(env).slice(0, 3)).toEqual([0, 6, 12]);
        expect(sirenOn(env)).toBe(false);
    });

    it('shows the "click to start the alarm" hint while autoplay is blocked', async () => {
        const env = await pagerEnv({ autoplayBlocked: true });
        await env.openSocket();
        await env.frame(pageFrame(lead()));
        expect(env.$('#pager-sound-hint').hidden).toBe(false);
    });
});

describe('chime (the interview day)', () => {
    const CHIME_HZ = 988;
    const call = (overrides = {}) => incident({
        id: 91,
        title: "Please grade Jose Pasante's exercise",
        details: 'He finished the Developer internship exercise at 10:47.',
        source: 'd8',
        source_url: 'https://interview.local/admin/sessions/20/grade',
        alarm: 'chime',
        channel_name: 'interview-day',
        message_id: 950,
        ...overrides,
    });
    const ctxOf = (env) => env.state.audioContexts[0];
    // When each scheduled chime starts (its first beep), relative to the alarm.
    const chimes = (env) => ctxOf(env).oscillators
        .filter((o) => o.frequency.value === CHIME_HZ)
        .map((o) => Math.round((o.startAt - 0.05) * 1000) / 1000)
        .filter((_, i) => i % 4 === 0);
    const masters = (env, gain) => ctxOf(env).gains.filter((g) => g.gain.value === gain
        && g.connect.mock.calls.some(([dest]) => dest === ctxOf(env).destination));
    const ringingCall = async (inc = call()) => {
        const env = await pagerEnv();
        await env.openSocket();
        await env.frame(pageFrame(inc));
        return env;
    };

    it('is a banner in the corner, not the full screen', async () => {
        const env = await ringingCall();

        expect(overlay(env).hidden).toBe(false);
        expect(overlay(env).classList.contains('pager-overlay--banner')).toBe(true);
        expect(overlay(env).getAttribute('aria-modal')).toBe('false');
        expect(env.$('#pager-heading-text').textContent).toBe("You're needed");
        expect(env.$('#pager-footer').textContent).toContain('stops the chime');
    });

    it('chimes every 10 s for the first minute, then every 20 s, quietly and with no siren', async () => {
        const env = await ringingCall();

        // Scheduled 180 s ahead: the one due at 180 s goes on the next top-up.
        expect(chimes(env)).toEqual([0, 10, 20, 30, 40, 50, 60, 80, 100, 120, 140, 160]);
        expect(env.eval('[0, 6, 7, 8].map((n) => PagerManager.cueOffset("chime", n))')).toEqual([0, 60, 80, 100]);
        expect(masters(env, 0.16)).toHaveLength(1);
        expect(masters(env, 0.5)).toHaveLength(0);
        expect(ctxOf(env).oscillators.every((o) => o.type === 'sine')).toBe(true);
        expect(ctxOf(env).bufferSources).toHaveLength(0);
    });

    it('reads as a call and links what it is about', async () => {
        const env = await ringingCall();
        const [row] = rows(env);

        expect(row.querySelector('.pager-incident-title').textContent).toBe("🔔 Please grade Jose Pasante's exercise");
        const open = row.querySelector('a.btn');
        expect(open.textContent).toBe('Open');
        expect(open.href).toBe('https://interview.local/admin/sessions/20/grade');
        expect(row.querySelector('[data-pager-ack]').className).toContain('btn-primary');
        expect(env.notifications[0].title).toBe("🔔 Please grade Jose Pasante's exercise");
    });

    it('goes full screen when an outage rings alongside it, and back to a banner after', async () => {
        const env = await ringingCall();
        await env.frame(pageFrame(incident()));

        expect(overlay(env).classList.contains('pager-overlay--banner')).toBe(false);
        expect(overlay(env).getAttribute('aria-modal')).toBe('true');
        expect(env.$('#pager-heading-text').textContent).toBe('You are being paged');
        expect(sirenOn(env)).toBe(true);
        expect(masters(env, 0.16)).toHaveLength(1);

        await env.frame(pageClearedFrame(incident()));
        expect(overlay(env).classList.contains('pager-overlay--banner')).toBe(true);
        expect(sirenOn(env)).toBe(false);
    });

    it('stops when acknowledged elsewhere, including the chimes already scheduled', async () => {
        const env = await ringingCall();
        await env.frame(pageClearedFrame(call()));

        expect(overlay(env).hidden).toBe(true);
        const [master] = masters(env, 0.16);
        expect(master.disconnect.mock.calls.length).toBeGreaterThanOrEqual(1);
        expect(ctxOf(env).oscillators.every((s) => s.stop.mock.calls.length >= 1)).toBe(true);
    });

    it('Acknowledge silences it at once, offline too, until the server re-sends it', async () => {
        const env = await pagerEnv({
            fetchRoutes: [{ match: '/oncall/incidents/91/ack', reply: new TypeError('Failed to fetch') }],
        });
        await env.openSocket();
        await env.frame(pageFrame(call()));
        const live = () => masters(env, 0.16).filter((g) => g.disconnect.mock.calls.length === 0);
        expect(live()).toHaveLength(1);

        env.$('[data-pager-ack="91"]').click();
        await env.flush();

        expect(overlay(env).hidden).toBe(false);
        expect(live()).toHaveLength(0);
        expect(env.$('[data-incident-id="91"] .pager-incident-silenced')).not.toBeNull();

        await env.frame(pageFrame(call(), { repeat: true }));
        expect(live()).toHaveLength(1);
    });
});

describe('Accept and Reject (a lead D8 offered to this person)', () => {
    const BELL_HZ = 2093;
    const KACHING_MP3 = '/audio/ka-ching.mp3';
    // serialize_incident() for one of D8's directed lead pages on a D8-Chat
    // that knows D8's answer endpoint (lead_response).
    const offer = (overrides = {}) => incident({
        id: 77,
        title: 'Website lead for you: Dana Whitfield at Meridian Health Systems',
        details: 'Filled in the Contact page form. Open to accept within 5 minutes.',
        source: 'd8',
        source_url: 'https://new-d8.local/marketing/lead-offers/31',
        alarm: 'ka-ching',
        channel_name: 'website-leads',
        message_id: 901,
        next_escalation_at: null,
        lead_response: true,
        ...overrides,
    });
    // The server's outcomes (lead_response.Outcome.to_dict()).
    const ACCEPTED = {
        ok: true, stopped: true, status: 'accepted', message: 'The lead is yours.',
        lead_url: 'https://new-d8.local/marketing/leads/12', code: null, incident_id: 77,
    };
    const REJECTED = {
        ok: true, stopped: true, status: 'rejected', message: 'It goes to the next person.',
        lead_url: null, code: null, incident_id: 77,
    };
    const ctxOf = (env) => env.state.audioContexts[0];
    // The ka-ching's master gains: 0.5 for the synthesized one, 1 for the recording.
    const masters = (env) => ctxOf(env).gains.filter((g) => [0.5, 1].includes(g.gain.value)
        && g.connect.mock.calls.some(([dest]) => dest === ctxOf(env).destination));
    const kachingOn = (env) => masters(env).some((g) => g.disconnect.mock.calls.length === 0);
    const answers = (env) => env.$$('[data-pager-answer]');
    const errorText = (env) => env.$('[data-incident-id="77"] .pager-incident-error');
    const lastToast = (env) => env.toasts.at(-1);

    async function offerEnv({ answer, action = 'accept', inc = offer(), ...opts } = {}) {
        const fetchRoutes = [...(opts.fetchRoutes || [])];
        if (answer !== undefined) fetchRoutes.push({ match: `/oncall/incidents/77/${action}`, reply: answer });
        const env = await pagerEnv({ ...opts, fetchRoutes });
        await env.openSocket();
        await env.frame(pageFrame(inc));
        return env;
    }

    it('offers Accept and Reject instead of Acknowledge, on a green screen', async () => {
        const env = await offerEnv();
        const [row] = rows(env);

        expect(answers(env).map((b) => [b.dataset.pagerAnswer, b.textContent])).toEqual([
            ['accept', 'Accept'], ['reject', 'Reject'],
        ]);
        expect(row.querySelector('[data-pager-ack]')).toBeNull();
        // The lead's own link comes after the answer.
        expect(row.querySelector('a.btn')).toBeNull();
        expect(overlay(env).classList.contains('pager-overlay--lead')).toBe(true);
        expect(overlay(env).classList.contains('pager-overlay--banner')).toBe(false);
        expect(env.$('#pager-heading-text').textContent).toBe('Someone wants to talk to us');
        expect(env.$('#pager-footer').textContent)
            .toBe('Accept and the lead is yours. Reject and it goes to the next person.');
        expect(kachingOn(env)).toBe(true);
    });

    it('Accept is one tap: it silences at once, sends it, then stops and links the lead', async () => {
        const gate = deferred();
        const env = await offerEnv({ answer: () => gate.promise });

        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        expect(kachingOn(env)).toBe(false);
        expect(answers(env).every((b) => b.disabled)).toBe(true);
        expect(overlay(env).hidden).toBe(false);
        expect(env.$('.pager-incident-silenced')).toBeNull();
        const [call] = env.fetches('/oncall/incidents/77/accept');
        expect(call.init).toMatchObject({
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': 'csrf-test-token', Accept: 'application/json' },
        });

        gate.resolve({ json: ACCEPTED });
        await env.flush();

        expect(overlay(env).hidden).toBe(true);
        expect(kachingOn(env)).toBe(false);
        expect(lastToast(env)).toMatchObject({ title: 'Lead accepted' });
        expect(lastToast(env).header).toContain('bg-success');
        const link = env.$('#toast-body-content a');
        expect(link.textContent).toBe('Open the lead in D8');
        expect(link.href).toBe('https://new-d8.local/marketing/leads/12');
        expect(link.target).toBe('_blank');
        expect(link.rel).toBe('noopener noreferrer');
        expect(env.$('#toast-body-content').textContent).toContain('The lead is yours.');
    });

    it('copes with D8 stopping the page before the answer comes back', async () => {
        const gate = deferred();
        const env = await offerEnv({ answer: () => gate.promise });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        await env.frame(pageClearedFrame(offer(), 'resolved'));
        expect(overlay(env).hidden).toBe(true);

        gate.resolve({ json: ACCEPTED });
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
        expect(lastToast(env).title).toBe('Lead accepted');
    });

    it('and with the answer first, then D8 stopping the page', async () => {
        const env = await offerEnv({ answer: { json: ACCEPTED } });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();
        expect(overlay(env).hidden).toBe(true);

        await env.frame(pageClearedFrame(offer(), 'resolved'));
        env.server.active = [];
        await env.tick(RESYNC_MS);
        expect(overlay(env).hidden).toBe(true);
        expect(env.toasts).toHaveLength(1);
    });

    it('Reject asks first, in the app\'s dialog, and is silent while it asks', async () => {
        const env = await offerEnv({ answer: { json: REJECTED }, action: 'reject' });

        env.$('[data-pager-answer="reject"]').click();
        await env.flush();

        expect(kachingOn(env)).toBe(false);
        expect(env.fetches('/oncall/incidents/77/reject')).toHaveLength(0);
        expect(env.$('#confirm-modal-body').textContent).toBe('Reject this lead? It goes to the next person.');
        expect(env.$('#confirm-modal-ok').textContent).toBe('Reject');

        env.$('#confirm-modal-ok').click();
        await env.flush();

        expect(env.fetches('/oncall/incidents/77/reject')).toHaveLength(1);
        expect(overlay(env).hidden).toBe(true);
        expect(lastToast(env)).toMatchObject({ title: 'Lead rejected', body: 'It goes to the next person.' });
    });

    it('Reject, then Cancel: silent here until the server re-sends the page', async () => {
        const env = await offerEnv();
        env.$('[data-pager-answer="reject"]').click();
        await env.flush();

        expect(kachingOn(env)).toBe(false);
        expect(env.$('[data-incident-id="77"] .pager-incident-silenced').textContent)
            .toBe('🔇 Silenced in this tab. It rings again until you accept or reject.');
        expect(answers(env).every((b) => !b.disabled)).toBe(true);

        await env.frame(pageFrame(offer(), { repeat: true }));
        expect(kachingOn(env)).toBe(true);
    });

    it('no connection: says so and rings again', async () => {
        const env = await offerEnv({ answer: new TypeError('Failed to fetch') });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        expect(errorText(env).hidden).toBe(false);
        expect(errorText(env).textContent).toBe("Couldn't reach D8-Chat (Failed to fetch). Try again.");
        expect(kachingOn(env)).toBe(true);
        expect(overlay(env).hidden).toBe(false);
        expect(answers(env).every((b) => !b.disabled)).toBe(true);
        expect(env.$('.pager-incident-silenced')).toBeNull();
    });

    it('D8 out of reach: the server\'s reason shows and it rings again', async () => {
        const env = await offerEnv({ answer: { status: 502, json: { error: "Couldn't reach D8. Try again." } } });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        expect(errorText(env).textContent).toBe("Couldn't reach D8. Try again.");
        expect(kachingOn(env)).toBe(true);
        expect(env.toasts).toHaveLength(0);
    });

    it('an error without a JSON body reports the status and rings again', async () => {
        const env = await offerEnv({ answer: { status: 504 } });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        expect(errorText(env).textContent).toBe('D8-Chat had a problem (HTTP 504). Try again.');
        expect(kachingOn(env)).toBe(true);
    });

    it('gives up after 8 s without an answer and rings again', async () => {
        const env = await offerEnv({ answer: () => new Promise(() => {}) });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();
        await env.tick(7000);
        expect(errorText(env).hidden).toBe(true);
        expect(kachingOn(env)).toBe(false);

        await env.tick(1000);
        expect(errorText(env).textContent).toBe("D8-Chat didn't answer in time. Try again.");
        expect(kachingOn(env)).toBe(true);
        expect(answers(env).every((b) => !b.disabled)).toBe(true);
    });

    it('a failure after D8 already stopped the page is a toast', async () => {
        const gate = deferred();
        const env = await offerEnv({ answer: () => gate.promise });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();
        await env.frame(pageClearedFrame(offer(), 'resolved'));

        gate.reject(new TypeError('Failed to fetch'));
        await env.flush();

        expect(overlay(env).hidden).toBe(true);
        expect(lastToast(env)).toMatchObject({
            title: "Couldn't send your answer",
            body: "Couldn't reach D8-Chat (Failed to fetch). Try again.",
        });
    });

    it('nothing left to answer (the offer closed): it stops and says why', async () => {
        const closed = {
            ok: false, stopped: true, status: null, lead_url: null, code: 'offer_closed',
            message: 'Ana accepted it first.', incident_id: 77,
        };
        const env = await offerEnv({ answer: { json: closed } });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        expect(overlay(env).hidden).toBe(true);
        expect(kachingOn(env)).toBe(false);
        expect(lastToast(env)).toMatchObject({ title: 'Nothing to answer', body: 'Ana accepted it first.' });
        expect(env.$('#toast-body-content a')).toBeNull();
    });

    it('a re-send while the answer is out stays silent with the buttons disabled', async () => {
        const gate = deferred();
        const env = await offerEnv({ answer: () => gate.promise });
        env.$('[data-pager-answer="accept"]').click();
        await env.flush();

        await env.frame(pageFrame(offer(), { repeat: true }));

        expect(kachingOn(env)).toBe(false);
        expect(answers(env).every((b) => b.disabled)).toBe(true);
        gate.resolve({ json: ACCEPTED });
        await env.flush();
        expect(overlay(env).hidden).toBe(true);
    });

    it('the incident card\'s Accept goes through htmx and lands here as the lead-response event', async () => {
        const card = `<div id="incident-card-77" class="incident-card incident-triggered incident-lead mt-2">
            <button type="button" id="card-accept" hx-post="/oncall/incidents/77/accept" hx-disabled-elt="this" hx-swap="none">Accept</button>
        </div>`;
        const env = await offerEnv({
            page: { extraMain: card },
            xhrRoutes: [{
                match: '/oncall/incidents/77/accept',
                reply: { status: 200, body: '', headers: { 'HX-Trigger': JSON.stringify({ 'lead-response': ACCEPTED }) } },
            }],
        });

        env.$('#card-accept').click();
        await env.flush();

        expect(env.xhrs('/oncall/incidents/77/accept')).toHaveLength(1);
        expect(overlay(env).hidden).toBe(true);
        expect(lastToast(env).title).toBe('Lead accepted');
        expect(env.$('#toast-body-content a').href).toBe('https://new-d8.local/marketing/leads/12');
    });

    it('without D8\'s answer endpoint a lead keeps Acknowledge and its link, still green', async () => {
        const env = await offerEnv({ inc: offer({ lead_response: false }) });
        const [row] = rows(env);

        expect(answers(env)).toHaveLength(0);
        expect(row.querySelector('[data-pager-ack="77"]').textContent).toBe('Acknowledge');
        expect(row.querySelector('a.btn').textContent).toBe('Open the lead in D8');
        expect(overlay(env).classList.contains('pager-overlay--lead')).toBe(true);
        expect(env.$('#pager-heading-text').textContent).toBe('Someone wants to talk to us');
        expect(env.$('#pager-footer').textContent).toContain('Acknowledging');
    });

    it('an outage stays red, and turns the screen red while it rings beside a lead', async () => {
        const env = await offerEnv();
        await env.frame(pageFrame(incident()));
        expect(overlay(env).classList.contains('pager-overlay--lead')).toBe(false);
        expect(env.$('#pager-heading-text').textContent).toBe('You are being paged');
        // Each row keeps its own buttons.
        expect(answers(env)).toHaveLength(2);
        expect(env.$('[data-pager-ack="42"]')).not.toBeNull();

        await env.frame(pageClearedFrame(incident()));
        expect(overlay(env).classList.contains('pager-overlay--lead')).toBe(true);
    });

    it('a ka-ching test page looks like a lead but only stops the test', async () => {
        const env = await pagerEnv();
        await env.openSocket();
        const frame = testPageFrame('test-1792245600-ka-ching');
        frame.incident.alarm = 'ka-ching';
        await env.frame(frame);

        expect(overlay(env).classList.contains('pager-overlay--lead')).toBe(true);
        expect(answers(env)).toHaveLength(0);
        expect(env.$('[data-pager-ack]').textContent).toBe('Stop test');
    });

    it('the desktop notification asks to accept or reject', async () => {
        const env = await offerEnv({ inc: offer({ details: null }) });
        expect(env.notifications[0].title).toBe('💰 Website lead for you: Dana Whitfield at Meridian Health Systems');
        expect(env.notifications[0].options.body).toBe('Someone wants to talk to us. Open D8-Chat to accept or reject.');
    });

    describe('the recorded ka-ching', () => {
        const withRecording = { fetchRoutes: [{ match: KACHING_MP3, reply: { body: 'ID3 fake mp3' } }] };
        const recordings = (env) => ctxOf(env).bufferSources.filter((s) => s.buffer?.decoded);
        const bells = (env) => ctxOf(env).oscillators.filter((o) => o.frequency.value === BELL_HZ);
        const at = (sources) => sources.map((s) => Math.round((s.startAt - 0.05) * 1000) / 1000);

        it('replaces the synthesized one as soon as it loads, every 6 s on the same beat', async () => {
            const env = await offerEnv(withRecording);

            const [call] = env.fetches(KACHING_MP3);
            expect(call.url).toBe('/audio/ka-ching.mp3?v=0123abcd');
            expect(ctxOf(env).decoded).toHaveLength(1);
            expect(at(recordings(env))).toEqual(Array.from({ length: 30 }, (_, n) => n * 6));
            recordings(env).forEach((s) => expect(s.stopAt - s.startAt).toBeCloseTo(1.55));
            // The synthesized ones it replaced are cut off (stopped again, on
            // top of their scheduled stop).
            expect(bells(env).length).toBeGreaterThan(0);
            expect(bells(env).every((o) => o.stop.mock.calls.length >= 2)).toBe(true);
            const live = masters(env).filter((g) => g.disconnect.mock.calls.length === 0);
            expect(live.map((g) => g.gain.value)).toEqual([1]);
        });

        it('plays from the first ka-ching when it loaded before the page', async () => {
            const env = await pagerEnv(withRecording);
            await env.openSocket();
            env.document.body.click(); // any click starts the audio, and the load
            await env.flush();
            expect(ctxOf(env).decoded).toHaveLength(1);

            await env.frame(pageFrame(offer()));

            expect(bells(env)).toHaveLength(0);
            expect(at(recordings(env)).slice(0, 3)).toEqual([0, 6, 12]);
        });

        it('is fetched once, however many pages ring', async () => {
            const env = await offerEnv(withRecording);
            await env.frame(pageFrame(offer({ id: 78 })));
            await env.frame(pageClearedFrame(offer()));
            await env.frame(pageClearedFrame(offer({ id: 78 })));
            await env.frame(pageFrame(offer({ id: 79 })));
            expect(env.fetches(KACHING_MP3)).toHaveLength(1);
        });

        it('falls back to the synthesized ka-ching when the file can\'t load', async () => {
            const env = await offerEnv();
            expect(env.fetches(KACHING_MP3)).toHaveLength(1);
            expect(recordings(env)).toHaveLength(0);
            expect(bells(env).length).toBeGreaterThan(0);
            expect(bells(env).every((o) => o.stop.mock.calls.length === 1)).toBe(true);
            expect(kachingOn(env)).toBe(true);
            expect(env.state.console.some((c) => c.level === 'warn' && String(c.args[0]).includes('synthesized ka-ching'))).toBe(true);
        });

        it('and when it can\'t be decoded', async () => {
            const env = await offerEnv({ ...withRecording, audioDecodeFails: true });
            expect(ctxOf(env).decoded).toHaveLength(0);
            expect(recordings(env)).toHaveLength(0);
            expect(bells(env).length).toBeGreaterThan(0);
            expect(bells(env).every((o) => o.stop.mock.calls.length === 1)).toBe(true);
            expect(kachingOn(env)).toBe(true);
        });

        it('stops when the lead is answered, including the ka-chings already scheduled', async () => {
            const env = await offerEnv({ ...withRecording, answer: { json: ACCEPTED } });
            env.$('[data-pager-answer="accept"]').click();
            await env.flush();

            expect(kachingOn(env)).toBe(false);
            expect(recordings(env).length).toBeGreaterThan(0);
            expect(recordings(env).every((s) => s.stop.mock.calls.length >= 2)).toBe(true);
        });
    });
});
