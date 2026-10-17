// chat.js connection handling: the reconnect status bar, the reload on an
// auth-rejected socket (1008 "Not authenticated"), resubscribing and the
// post-reconnect backfill, and the visibility/online-triggered reconnect.
import { describe, expect, it, vi } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    ENGINEER, ME, chatPage, conversationPane, dateSeparator, message, messageFrame, unreadBadge,
} from './helpers/fixtures.js';

const TYPING_SENDER = '<div id="typing-sender" ws-send hx-trigger="typing-event" style="display: none"></div>';

function rows() {
    // Ids deliberately out of order: the backfill cursor must be the highest
    // id on screen, not the last row.
    return [
        dateSeparator('Today'),
        message({ id: 101, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }),
        message({ id: 105, user: ME, createdAt: '2026-10-17T13:05:00' }),
        message({ id: 103, user: ENGINEER, createdAt: '2026-10-17T13:06:00' }),
    ].join('\n');
}

async function chatEnv(opts = {}) {
    const env = await createPage({
        html: chatPage({ pane: conversationPane({ rows: rows() }), extraMain: TYPING_SENDER, ...opts.page }),
        ...opts,
    });
    env.window.Math.random = () => 0.5;
    return env;
}

const bar = (env) => env.$('#connection-status-bar');

describe('wsClose handling', () => {
    it('reloads the page when the socket is rejected with 1008 "Not authenticated"', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1008, 'Not authenticated');
        await env.flush();
        expect(env.reloads).toBe(1);
        expect(bar(env).style.display).toBe('none');
    });

    it('does not reload for a 1008 with any other reason', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1008, 'Rate limited');
        await env.flush();
        expect(env.reloads).toBe(0);
        expect(bar(env).style.display).toBe('none');
    });

    it.each([1000, 1001, 1008])('does not show the reconnect bar for a %i close', async (code) => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(code);
        await env.flush();
        expect(bar(env).style.display).toBe('none');
        expect(env.reloads).toBe(0);
    });

    it.each([1005, 1006, 1011, 1012, 4000])('shows the reconnect bar for a %i close', async (code) => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(code);
        await env.flush();
        expect(bar(env).style.display).toBe('block');
        expect(env.reloads).toBe(0);
    });

    it('shows the reconnect bar on a socket error', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.error();
        await env.flush();
        expect(bar(env).style.display).toBe('block');
    });

    it('says "Connection restored." on reconnect, then hides and resets the bar after 2s', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        expect(bar(env).style.display).toBe('block');
        expect(bar(env).textContent).toBe('Connection restored.');
        expect(bar(env).classList.contains('bg-success')).toBe(true);
        await env.tick(2000);
        expect(bar(env).style.display).toBe('none');
        expect(bar(env).classList.contains('bg-warning')).toBe(true);
        expect(bar(env).textContent).toContain('Connection lost. Attempting to reconnect...');
    });

    it('leaves the bar alone on the first, non-reconnect open', async () => {
        const env = await chatEnv();
        await env.openSocket();
        expect(bar(env).style.display).toBe('none');
    });
});

describe('subscribe and backfill on (re)connect', () => {
    it('subscribes the socket to the open conversation on every open', async () => {
        const env = await chatEnv();
        await env.openSocket();
        const subscribes = env.socket.sentJson.filter((m) => m.type === 'subscribe');
        expect(subscribes.length).toBeGreaterThan(0);
        expect(subscribes.every((m) => m.conversation_id === 'channel_1')).toBe(true);

        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        expect(env.socket.sentJson).toContainEqual(
            expect.objectContaining({ type: 'subscribe', conversation_id: 'channel_1' }),
        );
    });

    it('does not backfill on the first open', async () => {
        const env = await chatEnv();
        await env.openSocket();
        expect(env.xhrs(/\/messages\/since\//)).toHaveLength(0);
        expect(env.xhrs('/chat/sidebar/unreads')).toHaveLength(0);
    });

    it('after a reconnect, fetches messages since the highest id on screen and the sidebar unreads', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        expect(env.xhrs().map((x) => `${x.method} ${x.url}`)).toEqual([
            'GET /chat/conversations/channel_1/messages/since/105',
        ]);
        // Both requests share document.body as their htmx source, so htmx
        // queues the sidebar catch-up until the message backfill completes.
        env.xhrs(/\/messages\/since\//)[0].respond(200, '');
        await env.flush();
        expect(env.xhrs().map((x) => `${x.method} ${x.url}`)).toEqual([
            'GET /chat/conversations/channel_1/messages/since/105',
            'GET /chat/sidebar/unreads',
        ]);
    });

    it('backfills from 0 when the conversation has no messages yet', async () => {
        const env = await chatEnv({ page: { pane: conversationPane({ rows: '' }) } });
        await env.openSocket();
        env.socket.error();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        expect(env.xhrs('/chat/conversations/channel_1/messages/since/0')).toHaveLength(1);
    });

    it('appends the backfilled messages and badges from the catch-up responses', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();

        const missed = [
            messageFrame({ id: 106, user: ENGINEER, createdAt: '2026-10-17T13:30:00', content: 'missed 1' }),
            messageFrame({ id: 107, user: ENGINEER, createdAt: '2026-10-17T13:31:00', content: 'missed 2' }),
        ].join('');
        env.xhrs(/\/messages\/since\/105$/)[0].respond(200, missed);
        env.xhrs('/chat/sidebar/unreads')[0].respond(200, unreadBadge({
            convIdStr: 'channel_5', count: 3, linkText: '# devops', hxGetUrl: '/chat/channel/5',
        }));
        await env.tick(50);

        const ids = env.$$('#message-list .message-container').map((el) => el.id);
        expect(ids).toEqual(['message-101', 'message-105', 'message-103', 'message-106', 'message-107']);
        expect(env.$('#unread-badge-channel_5').textContent.trim()).toBe('3');
    });

    it('a message delivered both live and by the backfill appears once', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        const m = { id: 106, user: ENGINEER, createdAt: '2026-10-17T13:30:00', content: 'both ways' };
        await env.frame(messageFrame(m));
        env.xhrs(/\/messages\/since\//)[0].respond(200, messageFrame(m));
        await env.tick(50);
        expect(env.$$('#message-list #message-106')).toHaveLength(1);
    });

    it('reopens the conversation when the backfill was truncated (X-D8-Catchup: truncated)', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        env.xhrs(/\/messages\/since\//)[0].respond(200, '', { 'X-D8-Catchup': 'truncated' });
        await env.tick(50);
        // The sidebar link for channel_1 is clicked, which re-fetches the pane.
        expect(env.xhrs('/chat/channel/1')).toHaveLength(1);
        expect(env.reloads).toBe(0);
    });

    it('reloads the page for a truncated backfill when the conversation has no sidebar link', async () => {
        const env = await chatEnv({ page: { pane: conversationPane({ convIdStr: 'channel_99', rows: rows() }) } });
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.tick(500);
        await env.openSocket();
        env.xhrs(/\/messages\/since\//)[0].respond(200, '', { 'X-D8-Catchup': 'truncated' });
        await env.tick(50);
        expect(env.reloads).toBe(1);
    });
});

describe('visibility / network triggered reconnect', () => {
    it('asks htmx-ws to reconnect when the tab becomes visible', async () => {
        const env = await chatEnv({ visible: false });
        const spy = vi.fn();
        env.window.d8WsReconnectNow = spy;
        await env.setVisible(true);
        expect(spy).toHaveBeenCalledTimes(1);
    });

    it('does not reconnect when the tab is hidden', async () => {
        const env = await chatEnv();
        const spy = vi.fn();
        env.window.d8WsReconnectNow = spy;
        await env.setVisible(false);
        expect(spy).not.toHaveBeenCalled();
    });

    it('asks htmx-ws to reconnect when the network comes back online', async () => {
        const env = await chatEnv();
        const spy = vi.fn();
        env.window.d8WsReconnectNow = spy;
        env.window.dispatchEvent(new env.window.Event('online'));
        expect(spy).toHaveBeenCalledTimes(1);
    });

    it('end to end: refocusing a tab whose socket dropped reconnects without waiting for the backoff', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1006);
        await env.flush();
        expect(env.sockets).toHaveLength(1);
        await env.setVisible(false);
        await env.setVisible(true);
        expect(env.sockets).toHaveLength(2);
    });

    it('end to end: a visible tab with a healthy socket does not reconnect', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.setVisible(true);
        env.window.dispatchEvent(new env.window.Event('online'));
        await env.flush();
        expect(env.sockets).toHaveLength(1);
    });
});
