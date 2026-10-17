// chat.js idle/away presence (initPresence): the browser reports real input
// activity to data-activity-url, flips to "away" after 15 idle minutes, and
// only re-stamps "active" when there was input since the last ping.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import { ME, chatPage, conversationPane, profileHeader } from './helpers/fixtures.js';

const MIN = 60 * 1000;

async function chatEnv(user = ME) {
    return createPage({
        html: chatPage({ user, pane: conversationPane(), extraMain: profileHeader(user) }),
    });
}

const pings = (env) => env.fetches('/profile/activity').map(({ init }) => new URLSearchParams(String(init.body)).get('state'));

function activity(env, type = 'mousemove') {
    const evt = type === 'keydown'
        ? new env.window.KeyboardEvent('keydown', { bubbles: true, key: 'a' })
        : new env.window.MouseEvent(type, { bubbles: true });
    env.document.dispatchEvent(evt);
}

describe('activity pings', () => {
    it('stamps "active" on load as a keepalive form POST', async () => {
        const env = await chatEnv();
        const calls = env.fetches('/profile/activity');
        expect(calls).toHaveLength(1);
        expect(calls[0].init.method).toBe('POST');
        expect(calls[0].init.keepalive).toBe(true);
        expect(calls[0].init.headers['Content-Type']).toBe('application/x-www-form-urlencoded');
        expect(String(calls[0].init.body)).toBe('state=active');
    });

    // POST /profile/activity is under Flask-WTF's global CSRFProtect, and this
    // fetch bypasses htmx's configRequest hook. Without the header every ping
    // was a 400, so auto-away and the activity-gated mobile push never saw
    // web activity.
    it('sends the CSRF token with the activity ping', async () => {
        const env = await chatEnv();
        const [{ init }] = env.fetches('/profile/activity');
        expect(init.headers['X-CSRFToken']).toBe('csrf-test-token');
    });

    it('does not ping on load when the user set themselves away', async () => {
        const env = await chatEnv({ ...ME, presence: 'away' });
        expect(pings(env)).toEqual([]);
    });

    it('pings "active" on load for a busy user (present, just not interruptible)', async () => {
        const env = await chatEnv({ ...ME, presence: 'busy' });
        expect(pings(env)).toEqual(['active']);
    });
});

describe('auto-away', () => {
    it('reports "away" once after 15 idle minutes', async () => {
        const env = await chatEnv();
        await env.tick(14.5 * MIN);
        expect(pings(env)).toEqual(['active']);
        await env.tick(0.5 * MIN);
        expect(pings(env)).toEqual(['active', 'away']);
        await env.tick(60 * MIN);
        expect(pings(env)).toEqual(['active', 'away']);
    });

    it('reports "active" again on the first input after going away', async () => {
        const env = await chatEnv();
        await env.tick(15 * MIN);
        activity(env, 'keydown');
        await env.flush();
        expect(pings(env)).toEqual(['active', 'away', 'active']);
        activity(env, 'mousemove');
        await env.flush();
        expect(pings(env)).toEqual(['active', 'away', 'active']);
    });

    it('keeps an active user online: input resets the idle clock', async () => {
        const env = await chatEnv();
        for (let i = 0; i < 6; i++) {
            await env.tick(10 * MIN);
            activity(env);
        }
        expect(pings(env)).not.toContain('away');
    });

    it.each(['busy', 'away'])('never auto-flips a user who chose %s', async (status) => {
        const env = await chatEnv({ ...ME, presence: status });
        const before = pings(env);
        await env.tick(60 * MIN);
        activity(env);
        await env.tick(10 * MIN);
        expect(pings(env)).toEqual(before);
    });

    it('follows a manual status change from the profile menu', async () => {
        const env = await chatEnv();
        env.$('[data-presence-choice="busy"]').click();
        await env.tick(30 * MIN);
        expect(pings(env)).toEqual(['active']);

        // Back to online: choosing counts as activity (one keepalive), then
        // auto-away resumes after 15 idle minutes.
        env.$('[data-presence-choice="online"]').click();
        await env.tick(15 * MIN);
        expect(pings(env)).toEqual(['active', 'active', 'away']);
    });
});

describe('keepalive', () => {
    it('re-stamps "active" every ~4 minutes while there is input', async () => {
        const env = await chatEnv();
        await env.tick(1 * MIN);
        activity(env);
        await env.tick(3 * MIN); // 4 min since the load ping
        expect(pings(env)).toEqual(['active', 'active']);
        await env.tick(2 * MIN);
        activity(env);
        await env.tick(2 * MIN);
        expect(pings(env)).toEqual(['active', 'active', 'active']);
    });

    it('lets an untouched tab go stale: no keepalive without input since the last ping', async () => {
        // Regression (1f1d86d): the keepalive used to re-stamp an idle tab, so
        // the server's push gate thought the user was at their desk.
        const env = await chatEnv();
        await env.tick(14 * MIN);
        expect(pings(env)).toEqual(['active']);
    });
});
