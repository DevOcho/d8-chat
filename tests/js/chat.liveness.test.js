// Keeping a page's realtime view honest without a refresh: the WebSocket
// heartbeat that catches a half-open socket, the backfill on the server's
// subscribe ack, dropping appends meant for another conversation's pane, and
// keeping the DM sidebar most-recent first.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    ENGINEER, ME, ROBERT, chatPage, conversationPane, dmBadgeFrame, dmConvId, message, messageFrame,
    messageListAppend,
} from './helpers/fixtures.js';

const TYPING_SENDER = '<div id="typing-sender" ws-send hx-trigger="typing-event" style="display: none"></div>';

async function chatEnv({ pane, dms, ...opts } = {}) {
    const env = await createPage({
        html: chatPage({
            pane: pane ?? conversationPane({
                rows: message({ id: 101, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }),
            }),
            extraMain: TYPING_SENDER,
            ...(dms ? { dms } : {}),
        }),
        ...opts,
    });
    env.window.Math.random = () => 0.5;
    return env;
}

const pings = (socket) => socket.sentJson.filter((m) => m.type === 'ping');
const bar = (env) => env.$('#connection-status-bar');
const dmOrder = (env) => env.$$('#dm-list > li').map((li) => li.id);

describe('heartbeat', () => {
    it('pings every 25 seconds while the socket is open', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.tick(25000);
        expect(pings(env.socket)).toHaveLength(1);
        await env.frame({ type: 'pong' });
        await env.tick(25000);
        expect(pings(env.socket)).toHaveLength(2);
    });

    it('keeps a socket that answers', async () => {
        const env = await chatEnv();
        await env.openSocket();
        for (let i = 0; i < 3; i++) {
            await env.tick(25000);
            await env.frame({ type: 'pong' });
        }
        await env.tick(10000);
        expect(pings(env.socket)).toHaveLength(3);
        expect(env.sockets).toHaveLength(1);
        expect(bar(env).style.display).toBe('none');
    });

    it('any frame counts as a reply, not only a pong', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.tick(25000);
        await env.frame({ type: 'typing_update', conversation_id: 'channel_1', typists: [] });
        await env.tick(10000);
        expect(env.sockets).toHaveLength(1);
    });

    it('replaces a socket that stays silent after a ping, and catches up once reconnected', async () => {
        const env = await chatEnv();
        await env.openSocket();
        const dead = env.socket;
        await env.tick(25000);
        await env.tick(10000);
        expect(dead.clientClose).not.toBeNull();
        expect(env.sockets).toHaveLength(2);
        expect(bar(env).style.display).toBe('block');

        await env.openSocket();
        expect(bar(env).textContent).toBe('Connection restored.');
        expect(env.xhrs(/\/messages\/since\/101$/)).toHaveLength(1);
        env.xhrs(/\/messages\/since\//)[0].respond(200, '');
        await env.flush();
        expect(env.xhrs('/chat/sidebar/unreads')).toHaveLength(1);
    });

    it('checks the socket on a short fuse when the tab becomes visible', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.setVisible(false);
        await env.setVisible(true);
        expect(pings(env.socket)).toHaveLength(1);
        await env.tick(5000);
        expect(env.sockets).toHaveLength(2);
    });

    it('stops pinging a closed socket', async () => {
        const env = await chatEnv();
        await env.openSocket();
        env.socket.serverClose(1000);
        await env.tick(60000);
        expect(pings(env.socket)).toHaveLength(0);
    });
});

describe('backfill on the subscribe ack', () => {
    it('fetches messages posted between the render and the subscribe', async () => {
        const env = await chatEnv();
        await env.openSocket();
        expect(env.xhrs(/\/messages\/since\//)).toHaveLength(0);
        await env.frame({ type: 'subscribed', conversation_id: 'channel_1' });
        expect(env.xhrs('/chat/conversations/channel_1/messages/since/101')).toHaveLength(1);
    });

    it('ignores an ack for a conversation that is no longer on screen', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.frame({ type: 'subscribed', conversation_id: 'channel_5' });
        expect(env.xhrs(/\/messages\/since\//)).toHaveLength(0);
    });

    it('does not append the newest messages under an older page', async () => {
        const rows = message({ id: 101, user: ENGINEER, createdAt: '2026-10-17T13:00:00' })
            + '<div class="text-center newer-message-loader"></div>';
        const env = await chatEnv({ pane: conversationPane({ rows }) });
        await env.openSocket();
        await env.frame({ type: 'subscribed', conversation_id: 'channel_1' });
        expect(env.xhrs(/\/messages\/since\//)).toHaveLength(0);
    });
});

describe('appends tagged with their conversation', () => {
    const m = { id: 120, user: ENGINEER, createdAt: '2026-10-17T13:10:00', content: 'elsewhere' };

    it('drops a message meant for another conversation', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.frame(messageFrame(m, { convIdStr: 'channel_5' }));
        expect(env.$('#message-120')).toBeNull();
    });

    it("appends the open conversation's message", async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.frame(messageFrame(m, { convIdStr: 'channel_1' }));
        expect(env.$('#message-list #message-120')).not.toBeNull();
    });

    it('still appends an untagged message from an older server', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.frame(messageListAppend(null, message(m)));
        expect(env.$('#message-list #message-120')).not.toBeNull();
    });
});

describe('DM sidebar order', () => {
    const selfConv = dmConvId(ME.id, ME.id);
    const engConv = dmConvId(ME.id, ENGINEER.id);
    const robertConv = dmConvId(ME.id, ROBERT.id);
    const twoDms = [{ user: ENGINEER }, { user: ROBERT }];

    it('puts a brand-new conversation at the top, under my own row', async () => {
        const env = await chatEnv();
        await env.openSocket();
        await env.frame(dmBadgeFrame({ sender: ROBERT, count: 1 }));
        expect(dmOrder(env)).toEqual(['dm-self-item', `dm-item-${robertConv}`, `dm-item-${engConv}`]);
        // The moved row still works.
        expect(env.$(`#dm-item-${robertConv} a`).getAttribute('hx-get')).toBe(`/chat/dm/${ROBERT.id}`);
    });

    it('moves an existing conversation to the top when a message arrives', async () => {
        const env = await chatEnv({ dms: twoDms });
        await env.openSocket();
        expect(dmOrder(env)).toEqual(['dm-self-item', `dm-item-${engConv}`, `dm-item-${robertConv}`]);
        await env.frame(dmBadgeFrame({ sender: ROBERT, count: 1 }));
        expect(dmOrder(env)).toEqual(['dm-self-item', `dm-item-${robertConv}`, `dm-item-${engConv}`]);
        expect(env.$$(`[id="dm-item-${robertConv}"]`)).toHaveLength(1);
    });

    it('moves the open DM to the top when a message lands in it', async () => {
        const env = await chatEnv({
            dms: twoDms,
            pane: conversationPane({ convIdStr: robertConv }),
        });
        await env.openSocket();
        await env.frame(messageFrame(
            { id: 130, user: ME, createdAt: '2026-10-17T13:20:00', content: 'hi robert' },
            { convIdStr: robertConv },
        ));
        expect(dmOrder(env)).toEqual(['dm-self-item', `dm-item-${robertConv}`, `dm-item-${engConv}`]);
        expect(selfConv).toBe(`dm_${ME.id}_${ME.id}`);
    });
});
