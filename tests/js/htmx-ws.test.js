// The vendored htmx WebSocket extension (app/static/js/local/htmx-ws.js) and
// its D8-PATCHes:
//   1. reconnect on any abnormal close code, never on 1000/1001/1008;
//   2. reset the backoff only after a socket has stayed open for 10s;
//   3. window.d8WsReconnectNow() resets the backoff and reconnects unless OPEN.
//
// Loaded with htmx alone (no chat.js) so only the extension's behaviour is
// under test. Math.random is pinned to 0.5, so the full-jitter reconnect delay
// for retry n is exactly 500 * 2^min(n, 6) ms.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';

const PAGE = `<!DOCTYPE html><html><head></head><body>
<main class="main-content" ws-connect="/ws/chat" hx-ext="ws"></main>
</body></html>`;

async function wsPage() {
    const env = await createPage({ html: PAGE, scripts: ['htmx', 'htmx-ws'] });
    env.window.Math.random = () => 0.5;
    const events = [];
    for (const name of ['htmx:wsOpen', 'htmx:wsClose', 'htmx:wsError', 'htmx:wsConnecting']) {
        env.document.body.addEventListener(name, (e) => events.push({ name, code: e.detail.event?.code }));
    }
    env.events = events;
    return env;
}

// Run timers one at a time until a new socket appears; returns how long that
// took on the fake clock (ms), or Infinity if none appears within `limit`.
async function timeToNextSocket(env, limit = 120000) {
    const before = env.sockets.length;
    const start = env.clock.now;
    while (env.sockets.length === before && env.clock.countTimers() > 0) {
        await env.clock.nextAsync();
        if (env.clock.now - start > limit) break;
    }
    return env.sockets.length === before ? Infinity : env.clock.now - start;
}

describe('htmx-ws: connecting', () => {
    it('opens a wss:// socket to the ws-connect path on the page origin', async () => {
        const env = await wsPage();
        expect(env.sockets).toHaveLength(1);
        expect(env.socket.url).toBe('wss://d8-chat.local/ws/chat');
        await env.openSocket();
        expect(env.events.map((e) => e.name)).toContain('htmx:wsOpen');
    });

    it('dispatches each frame as htmx:wsBeforeMessage then htmx:wsAfterMessage on the ws-connect element', async () => {
        const env = await wsPage();
        await env.openSocket();
        const seen = [];
        const main = env.$('main');
        main.addEventListener('htmx:wsBeforeMessage', (e) => seen.push(['before', e.detail.message]));
        main.addEventListener('htmx:wsAfterMessage', (e) => seen.push(['after', e.detail.message]));
        await env.frame({ type: 'sound' });
        expect(seen).toEqual([['before', '{"type":"sound"}'], ['after', '{"type":"sound"}']]);
    });

    it('queues ws-send messages until the socket opens, then flushes them', async () => {
        const env = await createPage({
            html: `<!DOCTYPE html><html><body><main ws-connect="/ws/chat" hx-ext="ws">
                <div ws-send hx-trigger="load" hx-vals='{"type": "subscribe", "conversation_id": "channel_1"}'></div>
            </main></body></html>`,
            scripts: ['htmx', 'htmx-ws'],
        });
        expect(env.socket.sent).toEqual([]);
        await env.openSocket();
        expect(env.socket.sentJson).toEqual([
            expect.objectContaining({ type: 'subscribe', conversation_id: 'channel_1' }),
        ]);
    });
});

describe('htmx-ws: reconnect on close (D8-PATCH 1)', () => {
    it.each([1002, 1003, 1005, 1006, 1007, 1011, 1012, 1013, 4000, 4401])(
        'reconnects after an abnormal close with code %i',
        async (code) => {
            const env = await wsPage();
            await env.openSocket();
            env.socket.serverClose(code);
            expect(await timeToNextSocket(env)).toBe(500);
            expect(env.events.find((e) => e.name === 'htmx:wsClose').code).toBe(code);
        },
    );

    it.each([
        [1000, 'normal closure'],
        [1001, 'going away'],
        [1008, 'policy violation / auth reject'],
    ])('does not reconnect after a %i (%s) close', async (code) => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(code);
        await env.tick(10 * 60 * 1000);
        expect(env.sockets).toHaveLength(1);
        // chat.js still hears about it, to decide on the reload / status bar.
        expect(env.events.filter((e) => e.name === 'htmx:wsClose')).toEqual([
            { name: 'htmx:wsClose', code },
        ]);
    });

    it('reconnects when the socket never managed to open (handshake failure, 1006)', async () => {
        const env = await wsPage();
        env.socket.error();
        env.socket.serverClose(1006);
        expect(await timeToNextSocket(env)).toBe(500);
        expect(env.events.map((e) => e.name)).toContain('htmx:wsError');
    });

    it('does not reconnect once the ws-connect element has left the page', async () => {
        const env = await wsPage();
        await env.openSocket();
        const socket = env.socket;
        env.$('main').remove();
        socket.serverClose(1006);
        await env.tick(120000);
        expect(env.sockets).toHaveLength(1);
    });
});

describe('htmx-ws: backoff (D8-PATCH 2)', () => {
    it('keeps backing off while sockets open and immediately drop', async () => {
        const env = await wsPage();
        const delays = [];
        for (let i = 0; i < 8; i++) {
            await env.openSocket();
            env.socket.serverClose(1006);
            delays.push(await timeToNextSocket(env));
        }
        // 2^n growth, capped at 2^6.
        expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 16000, 32000, 32000]);
    });

    it('does not reset the backoff for a socket that drops before 10s', async () => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env); // retry 0 → 500ms
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env); // retry 1 → 1000ms
        await env.openSocket();
        await env.tick(9990);
        env.socket.serverClose(1006);
        expect(await timeToNextSocket(env)).toBe(2000);
    });

    it('resets the backoff once a socket has stayed open for 10s', async () => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env);
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env);
        await env.openSocket();
        await env.tick(10000);
        env.socket.serverClose(1006);
        expect(await timeToNextSocket(env)).toBe(500);
    });

    it("an earlier socket's 10s timer does not reset the backoff for its replacement", async () => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env); // 500ms, retry → 1
        await env.openSocket(); // socket #2 opens at t; its 10s check fires at t+10s
        await env.tick(4000);
        env.socket.serverClose(1006);
        await timeToNextSocket(env); // 1000ms, retry → 2
        await env.openSocket(); // socket #3, opened at t+5s
        await env.tick(6000); // past socket #2's 10s mark, not socket #3's
        env.socket.serverClose(1006);
        expect(await timeToNextSocket(env)).toBe(2000);
    });
});

describe('htmx-ws: window.d8WsReconnectNow (D8-PATCH 3)', () => {
    it('is exposed once a ws-connect element is processed', async () => {
        const env = await wsPage();
        expect(typeof env.window.d8WsReconnectNow).toBe('function');
    });

    it('does nothing while the socket is OPEN', async () => {
        const env = await wsPage();
        await env.openSocket();
        env.window.d8WsReconnectNow();
        expect(env.sockets).toHaveLength(1);
        expect(env.socket.clientClose).toBeNull();
    });

    it('reconnects immediately after a drop instead of waiting out the backoff', async () => {
        const env = await wsPage();
        for (let i = 0; i < 5; i++) {
            await env.openSocket();
            env.socket.serverClose(1006);
            await timeToNextSocket(env);
        }
        await env.openSocket();
        env.socket.serverClose(1006); // next automatic retry would be 16s away
        env.window.d8WsReconnectNow();
        expect(env.sockets).toHaveLength(7);
        expect(env.socket.readyState).toBe(0);
    });

    it('resets the backoff, so a failure right after a manual reconnect retries in 500ms', async () => {
        const env = await wsPage();
        for (let i = 0; i < 5; i++) {
            await env.openSocket();
            env.socket.serverClose(1006);
            await timeToNextSocket(env);
        }
        env.socket.serverClose(1006); // still connecting; give up on it
        await env.flush();
        env.window.d8WsReconnectNow();
        env.socket.serverClose(1006);
        expect(await timeToNextSocket(env)).toBe(500);
    });

    // Regression (fixed): d8WsReconnectNow() only skips OPEN sockets, and the extension's
    // init() only closes the previous socket when it is OPEN. Called while a
    // reconnect attempt is still CONNECTING (tab refocused, or `online` fired,
    // during a slow handshake), it starts a second socket and leaves the first
    // one alive with all its handlers. If both connect, every server frame is
    // processed twice (duplicate notifications/sounds), and when the orphan
    // later closes its onclose schedules yet another reconnect.
    it('does not leave a second live socket when called during CONNECTING', async () => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(1006);
        await timeToNextSocket(env);
        const connecting = env.socket;
        expect(connecting.readyState).toBe(0);
        env.window.d8WsReconnectNow();
        const live = env.sockets.filter((s) => s.readyState < 2 && !s.clientClose);
        expect(live).toHaveLength(1);
    });

    // Regression (fixed): the backoff timer scheduled by the abnormal close keeps running
    // after d8WsReconnectNow() has already reconnected. When it fires, init()
    // closes the healthy OPEN socket and opens another one. The browser reports
    // that client-side close as 1005, which the D8-PATCH treats as abnormal, so
    // it schedules another reconnect that closes the next healthy socket, and
    // so on: a laptop that wakes up and refocuses the tab can churn its
    // connection indefinitely (backoff capped at 64s).
    it('a pending backoff timer does not tear down the socket d8WsReconnectNow opened', async () => {
        const env = await wsPage();
        await env.openSocket();
        env.socket.serverClose(1006); // reconnect timer armed (500ms)
        env.window.d8WsReconnectNow();
        const healthy = env.socket;
        await env.openSocket(healthy);
        await env.tick(1000); // the stale timer fires
        expect(healthy.clientClose).toBeNull();
        expect(env.sockets).toHaveLength(2);
    });
});
