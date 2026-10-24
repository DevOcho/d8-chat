// JSON WebSocket frames handled by chat.js's htmx:wsAfterMessage listener:
// presence, typing, avatar, desktop notifications, sounds, and the routing of
// on-call `page` / `page_cleared` frames to the pager.
import { describe, expect, it, vi } from 'vitest';
import { PAGE_SCRIPTS, createPage, isShown } from './helpers/env.js';
import {
    ENGINEER, ME, ROBERT, chatInputDefault, chatPage, conversationPane, incident, pageClearedFrame,
    pageFrame, preferenceReply, preferenceSelects, profileHeader,
} from './helpers/fixtures.js';

async function chatEnv({ page = {}, ...opts } = {}) {
    const env = await createPage({
        html: chatPage({
            pane: conversationPane(),
            dms: [
                { user: ENGINEER, presenceClass: 'presence-online' },
                { user: ROBERT, presenceClass: 'presence-away' },
            ],
            ...page,
        }),
        ...opts,
    });
    await env.openSocket();
    return env;
}

const presence = (env, sel) => [...env.$(sel).classList].find((c) => /^presence-(online|away|busy)$/.test(c));

describe('presence_update', () => {
    it("moves another user's sidebar dot to the new status", async () => {
        const env = await chatEnv();
        await env.frame({ type: 'presence_update', user_id: ROBERT.id, status_class: 'presence-online', status: 'online' });
        expect(presence(env, `#status-dot-${ROBERT.id}`)).toBe('presence-online');
        // Nobody else changes.
        expect(presence(env, `#status-dot-${ENGINEER.id}`)).toBe('presence-online');
        expect(presence(env, `#status-dot-${ME.id}`)).toBe('presence-online');
    });

    it('updates all three of my own dots: self-DM row, sidebar avatar and profile header', async () => {
        const env = await chatEnv({ page: { extraMain: profileHeader(ME) } });
        await env.frame({ type: 'presence_update', user_id: ME.id, status_class: 'presence-away', status: 'online' });
        expect(presence(env, `#status-dot-${ME.id}`)).toBe('presence-away');
        expect(presence(env, `#sidebar-presence-indicator-${ME.id}`)).toBe('presence-away');
        expect(presence(env, `#profile-presence-indicator-${ME.id}`)).toBe('presence-away');
        expect(env.$(`#status-dot-${ME.id}`).classList.contains('presence-indicator')).toBe(true);

        await env.frame({ type: 'presence_update', user_id: ME.id, status_class: 'presence-busy', status: 'busy' });
        expect(presence(env, `#sidebar-presence-indicator-${ME.id}`)).toBe('presence-busy');
    });

    it('renders my own dot from my chosen status on load (busy)', async () => {
        const env = await chatEnv({ page: { user: { ...ME, presence: 'busy' } } });
        expect(presence(env, `#status-dot-${ME.id}`)).toBe('presence-busy');
        expect(presence(env, `#sidebar-presence-indicator-${ME.id}`)).toBe('presence-busy');
    });

    it('ignores a presence_update for a user who is not on the page', async () => {
        const env = await chatEnv();
        await env.frame({ type: 'presence_update', user_id: 999, status_class: 'presence-online', status: 'online' });
        expect(env.state.errors).toEqual([]);
    });
});

describe('typing_update', () => {
    async function withComposer() {
        const env = await chatEnv({ page: { inputLoader: true }, xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault() } }] });
        await env.tick(50);
        return env;
    }

    it.each([
        [['engineer'], 'engineer is typing...'],
        [['engineer', 'robert'], 'engineer and robert are typing...'],
        [['engineer', 'robert', 'emilio'], 'Several people are typing...'],
        [[], ''],
    ])('typists %j → "%s"', async (typists, text) => {
        const env = await withComposer();
        await env.frame({ type: 'typing_update', typists });
        expect(env.$('#typing-indicator').textContent).toBe(text);
    });

    it('leaves me out of my own typing indicator', async () => {
        const env = await withComposer();
        await env.frame({ type: 'typing_update', typists: ['kp', 'engineer'] });
        expect(env.$('#typing-indicator').textContent).toBe('engineer is typing...');
        await env.frame({ type: 'typing_update', typists: ['kp'] });
        expect(env.$('#typing-indicator').textContent).toBe('');
    });
});

describe('avatar_update', () => {
    it('replaces initial placeholders with the new image and updates existing images', async () => {
        const env = await chatEnv();
        const before = env.$$(`.avatar-image[data-user-id="${ENGINEER.id}"]`);
        expect(before.every((el) => el.tagName === 'DIV')).toBe(true);
        const sizes = before.map((el) => [...el.classList].find((c) => /^avatar-\d+$/.test(c)));
        await env.frame({ type: 'avatar_update', user_id: ENGINEER.id, avatar_url: '/files/avatars/eng.png' });
        const avatars = env.$$(`.avatar-image[data-user-id="${ENGINEER.id}"]`);
        expect(avatars.length).toBeGreaterThan(0);
        expect(avatars.every((el) => el.tagName === 'IMG' && el.getAttribute('src') === '/files/avatars/eng.png')).toBe(true);
        // Each keeps its size class (no style attribute to copy; the CSP allows none).
        expect(avatars.map((el) => [...el.classList].find((c) => /^avatar-\d+$/.test(c)))).toEqual(sizes);
        expect(avatars.every((el) => !el.hasAttribute('style') && !el.classList.contains('avatar-initial'))).toBe(true);

        await env.frame({ type: 'avatar_update', user_id: ENGINEER.id, avatar_url: '/files/avatars/eng2.png' });
        expect(env.$$(`.avatar-image[data-user-id="${ENGINEER.id}"]`).map((el) => el.getAttribute('src')))
            .toEqual(avatars.map(() => '/files/avatars/eng2.png'));
        // Other users' avatars are untouched.
        expect(env.$(`.avatar-image[data-user-id="${ROBERT.id}"]`).tagName).toBe('DIV');
    });
});

describe('on-call frame routing', () => {
    it('hands page and page_cleared frames to PagerManager', async () => {
        const env = await chatEnv();
        const spy = vi.spyOn(env.window.PagerManager, 'handleFrame').mockImplementation(() => {});
        const page = pageFrame(incident());
        await env.frame(page);
        await env.frame(pageClearedFrame(incident()));
        expect(spy.mock.calls.map(([f]) => f.type)).toEqual(['page', 'page_cleared']);
        expect(spy.mock.calls[0][0]).toEqual(page);
    });

    it('does not treat a page as a chat notification or sound', async () => {
        const env = await chatEnv();
        await env.frame(pageFrame(incident()));
        // The pager's own sticky notification, not NotificationManager's.
        expect(env.notifications.map((n) => n.title)).toEqual(['🚨 DOWN: api.devocho.com']);
        expect(env.audios).toEqual([]);
        expect(env.toasts).toEqual([]);
    });

    it('ignores page frames on a page without the pager', async () => {
        const env = await createPage({
            html: chatPage({ pane: conversationPane() }),
            scripts: PAGE_SCRIPTS.filter((s) => s !== 'pager'),
        });
        await env.openSocket();
        await env.frame(pageFrame(incident()));
        expect(env.window.PagerManager).toBeUndefined();
        expect(env.notifications).toEqual([]);
    });
});

describe('notification frames', () => {
    const mention = {
        type: 'notification',
        title: 'New mention from Eng Ineer',
        body: '@kp the deploy is stuck',
        icon: '/favicon.ico',
        tag: 'channel_5',
    };

    it('shows a desktop notification when the window is not focused, closing it after 5s', async () => {
        const env = await chatEnv();
        await env.frame(mention);
        expect(env.notifications).toHaveLength(1);
        const [n] = env.notifications;
        expect(n.title).toBe(mention.title);
        expect(n.options).toEqual({ body: mention.body, icon: '/favicon.ico', tag: 'channel_5' });
        expect(env.toasts).toEqual([]);
        await env.tick(5000);
        expect(n.closed).toBe(true);
    });

    it('focuses the window and closes the notification when it is clicked', async () => {
        const env = await chatEnv();
        const focus = vi.fn();
        env.window.focus = focus;
        await env.frame(mention);
        env.notifications[0].onclick();
        expect(focus).toHaveBeenCalled();
        expect(env.notifications[0].closed).toBe(true);
    });

    it('falls back to an in-app toast when the window is focused', async () => {
        const env = await chatEnv({ focused: true });
        await env.frame(mention);
        expect(env.notifications).toEqual([]);
        expect(env.toasts).toEqual([expect.objectContaining({ title: mention.title, body: mention.body })]);
        expect(env.toasts[0].header).toContain('bg-primary');
    });

    it.each(['default', 'denied'])('falls back to a toast when notification permission is %s', async (permission) => {
        const env = await chatEnv({ notificationPermission: permission });
        await env.frame(mention);
        expect(env.notifications).toEqual([]);
        expect(env.toasts).toHaveLength(1);
    });

    it('offers the enable-notifications banner while permission is undecided', async () => {
        const env = await chatEnv({ notificationPermission: 'default' });
        expect(isShown(env.$('#enable-notifications-banner'))).toBe(true);
        env.state.notificationPermission = 'granted';
        env.$('#enable-notifications-btn').click();
        await env.flush();
        expect(env.window.Notification.requestPermission).toHaveBeenCalled();
        expect(isShown(env.$('#enable-notifications-banner'))).toBe(false);
    });
});

describe('sound frames', () => {
    it("plays the user's notification sound when the window is not focused", async () => {
        const env = await chatEnv();
        await env.frame({ type: 'sound' });
        expect(env.audios.map((a) => [a.src, a.played])).toEqual([['/audio/d8-notification.mp3', true]]);
    });

    it('stays quiet while the window is focused', async () => {
        const env = await chatEnv({ focused: true });
        await env.frame({ type: 'sound' });
        expect(env.audios).toEqual([]);
    });

    it('uses the sound chosen in preferences, including after a live change', async () => {
        const env = await chatEnv({
            page: {
                user: { ...ME, notificationSound: 'slack-notification.mp3' },
                extraMain: preferenceSelects({ sound: 'slack-notification.mp3' }),
            },
            xhrRoutes: [{
                match: '/profile/notification_sound',
                reply: (x) => preferenceReply('update-sound-preference', new URLSearchParams(x.body).get('sound')),
            }],
        });
        await env.frame({ type: 'sound' });
        expect(env.audios.at(-1).src).toBe('/audio/slack-notification.mp3');

        // Through htmx, as the Preferences select does: the route's
        // HX-Trigger reaches the listener as detail.value. (A hand-built event
        // in another shape once kept this test green while the page ignored
        // every live change.)
        const select = env.$('#notification-sound-select');
        select.value = 'd8-notification.mp3';
        select.dispatchEvent(new env.window.Event('change', { bubbles: true }));
        await env.tick(20);
        expect(env.xhrs(/notification_sound/).map((x) => x.method)).toEqual(['PUT']);
        await env.frame({ type: 'sound' });
        expect(env.audios.at(-1).src).toBe('/audio/d8-notification.mp3');
    });
});
