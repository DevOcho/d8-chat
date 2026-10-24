// Keyboard and polish: the Ctrl/Cmd+K quick switcher, Alt+Up/Down between
// conversations, Esc out of an edit or quote-reply, Up-arrow to edit your last
// message, the in-app confirm dialog for hx-confirm, and reaction pills that
// flip on click.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    ENGINEER, ME, ROBERT, chatInputDefault, chatPage, conversationPane, message, unreadBadge,
} from './helpers/fixtures.js';

const ROWS = [
    message({ id: 101, user: ME, createdAt: '2026-10-17T13:00:00', content: 'mine, older' }),
    message({ id: 102, user: ENGINEER, createdAt: '2026-10-17T13:01:00', content: 'theirs' }),
    message({ id: 103, user: ME, createdAt: '2026-10-17T13:02:00', content: 'mine, newest' }),
    message({ id: 104, user: ENGINEER, createdAt: '2026-10-17T13:03:00', content: 'theirs again' }),
].join('\n');

async function chatEnv(opts = {}) {
    const env = await createPage({
        html: chatPage({
            pane: conversationPane({ convIdStr: 'channel_1', rows: ROWS }),
            inputLoader: true,
            dms: [{ user: ENGINEER }, { user: ROBERT }],
            ...opts.page,
        }),
        xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault() } }, ...(opts.routes || [])],
    });
    await env.openSocket();
    await env.tick(50);
    return env;
}

const press = (env, target, key, extra = {}) => {
    target.dispatchEvent(new env.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra }));
};
const switcher = (env) => env.$('#quick-switcher');
const options = (env) => env.$$('#quick-switcher-results .quick-switcher-item').map((li) => li.textContent);

describe('quick switcher (Ctrl/Cmd+K)', () => {
    it('opens on Ctrl+K and on Cmd+K, and closes on Esc', async () => {
        const env = await chatEnv();
        press(env, env.document.body, 'k', { ctrlKey: true });
        expect(switcher(env).hidden).toBe(false);
        expect(env.document.activeElement.id).toBe('quick-switcher-input');
        press(env, env.$('#quick-switcher-input'), 'Escape');
        expect(switcher(env).hidden).toBe(true);
        press(env, env.document.body, 'k', { metaKey: true });
        expect(switcher(env).hidden).toBe(false);
    });

    it('lists unread conversations first, then filters as you type', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ convIdStr: 'dm_1_3', count: 1, linkText: 'Robert', hxGetUrl: '/chat/dm/3' }));
        press(env, env.document.body, 'k', { ctrlKey: true });
        expect(options(env)[0]).toBe('Robert');
        const input = env.$('#quick-switcher-input');
        input.value = 'dev';
        input.dispatchEvent(new env.window.Event('input'));
        expect(options(env)).toEqual(['# devops']);
    });

    it('opens the chosen conversation on Enter', async () => {
        const env = await chatEnv();
        press(env, env.document.body, 'k', { ctrlKey: true });
        const input = env.$('#quick-switcher-input');
        input.value = 'eng';
        input.dispatchEvent(new env.window.Event('input'));
        press(env, input, 'Enter');
        await env.flush();
        expect(switcher(env).hidden).toBe(true);
        expect(env.xhrs('/chat/dm/2')).toHaveLength(1);
    });

    it('moves the selection with the arrow keys', async () => {
        const env = await chatEnv();
        press(env, env.document.body, 'k', { ctrlKey: true });
        const input = env.$('#quick-switcher-input');
        press(env, input, 'ArrowDown');
        const active = env.$('#quick-switcher-results .active');
        expect(active.textContent).toBe(options(env)[1]);
        expect(input.getAttribute('aria-activedescendant')).toBe(active.id);
    });

    it('searches messages when nothing matches', async () => {
        const env = await chatEnv();
        press(env, env.document.body, 'k', { ctrlKey: true });
        const input = env.$('#quick-switcher-input');
        input.value = 'quarterly numbers';
        input.dispatchEvent(new env.window.Event('input'));
        expect(env.$('.quick-switcher-empty').textContent).toContain('quarterly numbers');
        press(env, input, 'Enter');
        await env.flush();
        expect(env.$('#global-search-input').value).toBe('quarterly numbers');
        expect(env.xhrs(/\/chat\/search/)).toHaveLength(1);
    });
});

describe('Alt+Up / Alt+Down', () => {
    it('steps through the sidebar from the open conversation', async () => {
        const env = await chatEnv();
        env.$('#channel-item-1 a').classList.add('active');
        press(env, env.document.body, 'ArrowDown', { altKey: true });
        await env.flush();
        expect(env.xhrs('/chat/channel/5')).toHaveLength(1);
    });

    it('with Shift, jumps to the next unread conversation', async () => {
        const env = await chatEnv();
        env.$('#channel-item-1 a').classList.add('active');
        await env.frame(unreadBadge({ convIdStr: 'dm_1_3', count: 1, linkText: 'Robert', hxGetUrl: '/chat/dm/3' }));
        press(env, env.document.body, 'ArrowDown', { altKey: true, shiftKey: true });
        await env.flush();
        expect(env.xhrs('/chat/channel/5')).toHaveLength(0);
        expect(env.xhrs('/chat/dm/3')).toHaveLength(1);
    });
});

describe('composer shortcuts', () => {
    it('Up-arrow in an empty composer edits your newest message, not just a last-child one', async () => {
        const env = await chatEnv();
        press(env, env.$('#wysiwyg-editor'), 'ArrowUp');
        await env.flush();
        // message-103 is mine; 104 (theirs) is the last row.
        expect(env.xhrs('/chat/message/103/load_for_edit')).toHaveLength(1);
    });

    it('Esc leaves a quote-reply or edit', async () => {
        const env = await chatEnv();
        const container = env.$('#chat-input-container');
        const cancel = env.document.createElement('button');
        cancel.className = 'btn-close';
        cancel.setAttribute('hx-get', '/chat/input/default');
        cancel.setAttribute('hx-target', '#chat-input-container');
        cancel.setAttribute('hx-swap', 'outerHTML');
        container.prepend(cancel);
        env.window.htmx.process(cancel);
        const before = env.xhrs('/chat/input/default').length;
        press(env, env.document.body, 'Escape');
        await env.flush();
        expect(env.xhrs('/chat/input/default').length).toBe(before + 1);
    });
});

describe('confirm dialog', () => {
    it('asks in the app instead of the browser, and sends only once confirmed', async () => {
        const env = await chatEnv();
        const btn = env.document.createElement('button');
        btn.setAttribute('hx-delete', '/chat/message/103');
        btn.setAttribute('hx-confirm', 'Are you sure you want to delete this message?');
        btn.setAttribute('hx-swap', 'none');
        env.document.body.appendChild(btn);
        env.window.htmx.process(btn);
        btn.click();
        await env.flush();
        expect(env.$('#confirm-modal-body').textContent).toBe('Are you sure you want to delete this message?');
        expect(env.$('#confirm-modal-ok').textContent).toBe('Delete');
        expect(env.xhrs('/chat/message/103').filter((x) => x.method === 'DELETE')).toHaveLength(0);
        env.$('#confirm-modal-ok').click();
        await env.flush();
        expect(env.xhrs('/chat/message/103').filter((x) => x.method === 'DELETE')).toHaveLength(1);
    });
});

describe('reaction pills', () => {
    it('flip as soon as they are clicked', async () => {
        const env = await chatEnv();
        env.$('#reactions-container-102').innerHTML = `
            <button class="btn btn-sm reaction-pill" data-reactor-ids="2" hx-post="/chat/message/102/react"
                    hx-vals='{"emoji": "👍"}' hx-swap="innerHTML">
                <span>👍</span><span class="reaction-count">1</span>
            </button>`;
        env.window.htmx.process(env.$('#reactions-container-102'));
        const pill = env.$('#reactions-container-102 .reaction-pill');
        pill.click();
        await env.flush();
        expect(pill.classList.contains('user-reacted')).toBe(true);
        expect(pill.querySelector('.reaction-count').textContent).toBe('2');
    });
});

describe('global search form', () => {
    it('never submits (it searches as you type)', async () => {
        const env = await chatEnv();
        const form = env.$('#global-search-form');
        const submit = new env.window.Event('submit', { bubbles: true, cancelable: true });
        form.dispatchEvent(submit);
        expect(submit.defaultPrevented).toBe(true);
    });
});
