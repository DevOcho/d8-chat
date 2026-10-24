// The "feels instant" layer: optimistic sends (a message shows before the
// server answers and is swapped for the server's copy by client_id), failed
// sends with Retry/Edit/Discard, conversation switching (sidebar highlight on
// click, kept composer, per-conversation drafts), the "N new messages" pill,
// and the page title.
import { describe, expect, it } from 'vitest';
import { createPage, isShown } from './helpers/env.js';
import {
    ENGINEER, ME, chatInputDefault, chatPage, conversationPane, message, messageFrame, messageListAppend,
    unreadBadge,
} from './helpers/fixtures.js';

const ROWS = message({ id: 101, user: ENGINEER, createdAt: '2026-10-17T13:00:00', content: 'earlier' });

async function chatEnv({ pane = conversationPane({ convIdStr: 'channel_1', rows: ROWS }), routes = [], ...opts } = {}) {
    const env = await createPage({
        html: chatPage({ pane, inputLoader: true, user: { ...ME } }),
        xhrRoutes: [
            { match: '/chat/input/default', reply: { body: chatInputDefault() } },
            ...routes,
        ],
        ...opts,
    });
    await env.openSocket();
    await env.tick(50);
    env.editor = () => env.$('#wysiwyg-editor');
    return env;
}

async function type(env, text) {
    const editor = env.editor();
    editor.textContent = text;
    editor.dispatchEvent(new env.window.Event('input', { bubbles: true }));
    await env.flush();
}

async function send(env, text) {
    await type(env, text);
    env.editor().dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await env.flush();
    const posts = env.xhrs(/\/messages$/).filter((x) => x.method === 'POST');
    return posts[posts.length - 1];
}

const clientIdOf = (post) => new URLSearchParams(post.body).get('client_id');
const serverCopy = (id, clientId, content, convIdStr = 'channel_1') => messageListAppend(
    convIdStr,
    message({ id, user: ME, createdAt: '2026-10-17T14:00:00', content, clientId }),
);

describe('optimistic send', () => {
    it('shows the message before the server answers', async () => {
        const env = await chatEnv();
        const post = await send(env, 'on its way');
        const pending = env.$('#message-list .pending-message');
        expect(pending).not.toBeNull();
        expect(pending.querySelector('.pending-text').textContent).toBe('on its way');
        expect(pending.dataset.clientId).toBe(clientIdOf(post));
        expect(clientIdOf(post)).toMatch(/^[A-Za-z0-9-]{1,64}$/);
    });

    it("renders the text as text, never as markup", async () => {
        const env = await chatEnv();
        await send(env, '<img src=x onerror=alert(1)>');
        const pending = env.$('#message-list .pending-message');
        expect(pending.querySelector('img[src="x"]')).toBeNull();
        expect(pending.querySelector('.pending-text').textContent).toBe('<img src=x onerror=alert(1)>');
    });

    it("is replaced by the server's copy from the POST response", async () => {
        const env = await chatEnv();
        const post = await send(env, 'hello');
        post.respond(200, serverCopy(500, clientIdOf(post), 'hello'));
        await env.tick(50);
        expect(env.$('.pending-message')).toBeNull();
        expect(env.$$('#message-list #message-500')).toHaveLength(1);
    });

    it('is replaced by the WebSocket echo when that comes first, and not duplicated after', async () => {
        const env = await chatEnv();
        const post = await send(env, 'hello');
        const echo = serverCopy(500, clientIdOf(post), 'hello');
        await env.frame(echo);
        expect(env.$('.pending-message')).toBeNull();
        post.respond(200, echo);
        await env.tick(50);
        expect(env.$$('#message-list #message-500')).toHaveLength(1);
    });

    it('stays below messages that arrive while it is in flight', async () => {
        const env = await chatEnv();
        await send(env, 'mine');
        await env.frame(messageFrame({ id: 102, user: ENGINEER, createdAt: '2026-10-17T13:59:00', content: 'theirs' }));
        const rows = env.$$('#message-list > .message-container').map((el) => el.id);
        expect(rows[rows.length - 1]).toMatch(/^pending-/);
    });

    it('does not wipe text typed while the send is in flight', async () => {
        const env = await chatEnv();
        const post = await send(env, 'first');
        await type(env, 'second, still typing');
        post.respond(200, serverCopy(500, clientIdOf(post), 'first'));
        await env.tick(50);
        expect(env.editor().textContent).toBe('second, still typing');
    });

    it('Retry re-sends the same message with the same client_id', async () => {
        const env = await chatEnv();
        const post = await send(env, 'try again');
        post.respond(500, '');
        await env.tick(50);
        env.$('[data-pending-action="retry"]').click();
        await env.flush();
        const posts = env.xhrs(/\/messages$/).filter((x) => x.method === 'POST');
        expect(posts).toHaveLength(2);
        const params = new URLSearchParams(posts[1].body);
        expect(params.get('chat_message')).toBe('try again');
        expect(params.get('client_id')).toBe(clientIdOf(post));
        expect(env.$('.pending-message').classList.contains('failed')).toBe(false);

        posts[1].respond(200, serverCopy(501, clientIdOf(post), 'try again'));
        await env.tick(50);
        expect(env.$('.pending-message')).toBeNull();
        expect(env.$('#message-501')).not.toBeNull();
    });

    it('Edit puts the text back in the composer; Discard just drops it', async () => {
        const env = await chatEnv();
        const first = await send(env, 'fix me');
        first.respond(500, '');
        await env.tick(50);
        env.$('[data-pending-action="edit"]').click();
        await env.flush();
        expect(env.$('.pending-message')).toBeNull();
        expect(env.$('#markdown-toggle-view').value).toBe('fix me');

        env.$('#wysiwyg-editor').textContent = '';
        const second = await send(env, 'never mind');
        second.respond(500, '');
        await env.tick(50);
        env.$('[data-pending-action="discard"]').click();
        expect(env.$('.pending-message')).toBeNull();
    });

    it('says when there was no connection', async () => {
        const env = await chatEnv();
        const post = await send(env, 'offline');
        post.fail();
        await env.tick(50);
        expect(env.$('.pending-message .pending-status').textContent).toContain('no connection');
    });
});

describe('conversation switching', () => {
    const DEVOPS_PANE = conversationPane({ convIdStr: 'channel_5', rows: message({ id: 201, user: ENGINEER, createdAt: '2026-10-17T12:00:00', content: 'devops' }) });
    const GENERAL_PANE = conversationPane({ convIdStr: 'channel_1', rows: ROWS });
    const switchRoutes = [
        { match: '/chat/channel/5', reply: { body: DEVOPS_PANE + chatInputDefault().replace('id="chat-input-container"', 'id="chat-input-container" hx-swap-oob="true"') } },
        { match: '/chat/channel/1', reply: { body: GENERAL_PANE + chatInputDefault().replace('id="chat-input-container"', 'id="chat-input-container" hx-swap-oob="true"') } },
    ];
    const open = async (env, channelId) => {
        env.$(`#channel-item-${channelId} a`).click();
        await env.tick(50);
    };

    it('highlights the clicked conversation at once', async () => {
        const env = await chatEnv({ routes: [] });
        env.$('#channel-item-5 a').click();
        // No response yet.
        expect(env.$('#channel-item-5 a').classList.contains('active')).toBe(true);
        expect(env.$('#channel-item-5 a').getAttribute('aria-current')).toBe('page');
    });

    it('keeps the composer instead of rebuilding it', async () => {
        const env = await chatEnv({ routes: switchRoutes });
        const editorBefore = env.editor();
        await open(env, 5);
        expect(env.$('[data-conversation-id="channel_5"]')).not.toBeNull();
        expect(env.editor()).toBe(editorBefore);
        expect(env.$$('[id="chat-input-container"]')).toHaveLength(1);
        // And still sends: a second editor bound to the kept form used to
        // find it already cleared and cancel the request.
        const post = await send(env, 'after switching');
        expect(post).toBeDefined();
        expect(new URLSearchParams(post.body).get('chat_message')).toBe('after switching');
        expect(env.xhrs(/\/messages$/).filter((x) => x.method === 'POST')).toHaveLength(1);
    });

    it('keeps a draft per conversation and marks it in the sidebar', async () => {
        const env = await chatEnv({ routes: switchRoutes });
        await type(env, 'half a thought');
        await env.tick(500);
        await open(env, 5);
        expect(env.editor().textContent).toBe('');
        expect(env.$('#link-channel_1').classList.contains('has-draft')).toBe(true);

        await open(env, 1);
        await env.tick(50);
        expect(env.$('#markdown-toggle-view').value).toBe('half a thought');
    });

    it('clears the draft once it is sent', async () => {
        const env = await chatEnv();
        await type(env, 'draft then send');
        await env.tick(500);
        expect(env.$('#link-channel_1').classList.contains('has-draft')).toBe(true);
        await send(env, 'draft then send');
        expect(env.$('#link-channel_1').classList.contains('has-draft')).toBe(false);
        expect(env.eval('DraftManager.get("channel_1")')).toBe('');
    });

    it('names the conversation in the composer and the page title', async () => {
        const env = await chatEnv({ routes: switchRoutes });
        await open(env, 5);
        expect(env.editor().dataset.placeholder).toBe('Message # devops');
        expect(env.document.title).toBe('# devops · D8 Chat');
        await env.frame(unreadBadge({ convIdStr: 'channel_1', count: 2, linkText: '# general', hxGetUrl: '/chat/channel/1' }));
        expect(env.document.title).toBe('(2) # devops · D8 Chat');
    });
});

describe('new messages pill', () => {
    function scrolledUp(env) {
        const container = env.$('#chat-messages-container');
        Object.defineProperty(container, 'scrollHeight', { configurable: true, get: () => 5000 });
        Object.defineProperty(container, 'clientHeight', { configurable: true, get: () => 500 });
        container.scrollTop = 1000;
        container.dispatchEvent(new env.window.Event('scroll'));
        return container;
    }
    const pill = (env) => env.$('#jump-to-bottom-btn');

    it("counts other people's messages while scrolled up", async () => {
        const env = await chatEnv();
        scrolledUp(env);
        await env.frame(messageFrame({ id: 102, user: ENGINEER, createdAt: '2026-10-17T13:01:00' }));
        await env.frame(messageFrame({ id: 103, user: ENGINEER, createdAt: '2026-10-17T13:02:00' }));
        expect(isShown(pill(env))).toBe(true);
        expect(pill(env).querySelector('.jump-label').textContent).toBe('2 new messages');
    });

    it('survives the pane being replaced (it lives outside the scroller)', async () => {
        const env = await chatEnv();
        expect(pill(env).closest('#chat-messages-container')).toBeNull();
    });

    it('clears when scrolled to the bottom', async () => {
        const env = await chatEnv();
        const container = scrolledUp(env);
        await env.frame(messageFrame({ id: 102, user: ENGINEER, createdAt: '2026-10-17T13:01:00' }));
        container.scrollTop = 4500;
        container.dispatchEvent(new env.window.Event('scroll'));
        expect(isShown(pill(env))).toBe(false);
    });
});
