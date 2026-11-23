// Message context menu (Alpine.js CSP), shared ContextMenuEmojiPicker,
// emoji-picker theme sync & search autofocus, and composer autofocus on
// channel switch.
import { describe, expect, it, vi } from 'vitest';
import { createPage, isShown } from './helpers/env.js';
import { ENGINEER, ME, chatInputDefault, chatPage, conversationPane, message } from './helpers/fixtures.js';

const MSG_1 = message({ id: 101, user: ME, createdAt: '2026-10-17T13:00:00', content: 'first' });
const MSG_2 = message({ id: 102, user: ENGINEER, createdAt: '2026-10-17T13:01:00', content: 'second' });

async function chatEnv({ routes = [], rows = `${MSG_1}${MSG_2}`, wysiwyg = true } = {}) {
    const env = await createPage({
        html: chatPage({ pane: conversationPane({ convIdStr: 'channel_1', rows }), inputLoader: true }),
        xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault({ wysiwyg }) } }, ...routes],
    });
    await env.openSocket();
    await env.tick(50);
    return env;
}

function rightClick(env, el, clientX = 100, clientY = 120) {
    el.dispatchEvent(new env.window.MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX,
        clientY,
    }));
}

async function settleAlpine(env) {
    await new Promise((r) => env.window.requestAnimationFrame(() => env.window.requestAnimationFrame(r)));
    await env.tick(200);
}

describe('messageContextMenu (Alpine CSP)', () => {
    it('opens on right-click and clamps position within the viewport', async () => {
        const env = await chatEnv();
        const msg = env.$('#message-101');
        const menu = msg.querySelector('[x-ref="menu"]');
        menu.getBoundingClientRect = () => ({ width: 210, height: 260 });

        Object.defineProperty(env.window, 'innerWidth', { configurable: true, value: 800 });
        Object.defineProperty(env.window, 'innerHeight', { configurable: true, value: 600 });

        rightClick(env, msg, 790, 590);
        await settleAlpine(env);

        expect(menu.style.display).not.toBe('none');
        expect(menu.style.left).toBe(`${800 - 210 - 8}px`);
        expect(menu.style.top).toBe(`${600 - 260 - 8}px`);
    });

    it('closes the first context menu when a second message is right-clicked', async () => {
        const env = await chatEnv();
        const msg1 = env.$('#message-101');
        const msg2 = env.$('#message-102');
        const menu1 = msg1.querySelector('[x-ref="menu"]');
        const menu2 = msg2.querySelector('[x-ref="menu"]');

        rightClick(env, msg1, 50, 50);
        await settleAlpine(env);
        expect(menu1.style.display).not.toBe('none');

        rightClick(env, msg2, 120, 140);
        await settleAlpine(env);
        expect(menu1.style.display).toBe('none');
        expect(menu2.style.display).not.toBe('none');
    });

    it('closes on Escape and on outside click', async () => {
        const env = await chatEnv();
        const msg = env.$('#message-101');
        const menu = msg.querySelector('[x-ref="menu"]');

        rightClick(env, msg, 50, 50);
        await settleAlpine(env);
        expect(menu.style.display).not.toBe('none');

        env.window.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await settleAlpine(env);
        expect(menu.style.display).toBe('none');

        rightClick(env, msg, 50, 50);
        await settleAlpine(env);
        expect(menu.style.display).not.toBe('none');

        Object.defineProperty(msg, 'offsetWidth', { configurable: true, value: 100 });
        Object.defineProperty(msg, 'offsetHeight', { configurable: true, value: 40 });
        env.$('#chat-header-container').dispatchEvent(new env.window.MouseEvent('click', { bubbles: true }));
        await settleAlpine(env);
        expect(menu.style.display).toBe('none');
    });

    it('closes the menu and opens the shared floating emoji picker on Add Reaction', async () => {
        const env = await chatEnv();
        const msg = env.$('#message-101');
        const menu = msg.querySelector('[x-ref="menu"]');
        const addBtn = menu.querySelector('button[title="Add Reaction"]');

        rightClick(env, msg, 60, 80);
        await settleAlpine(env);
        expect(menu.style.display).not.toBe('none');

        addBtn.dispatchEvent(new env.window.MouseEvent('click', { bubbles: true }));
        await settleAlpine(env);
        expect(menu.style.display).toBe('none');
        expect(env.$('#context-menu-emoji-picker').classList.contains('is-hidden')).toBe(false);
    });
});

describe('ContextMenuEmojiPicker', () => {
    it('opens clamped inside the viewport, syncs theme, and posts reaction on emoji-click', async () => {
        const env = await chatEnv({
            routes: [{ match: '/chat/message/101/react', reply: { body: '' } }],
        });
        env.document.documentElement.setAttribute('data-bs-theme', 'dark');

        const container = env.$('#context-menu-emoji-picker');
        const picker = container.querySelector('emoji-picker');
        container.getBoundingClientRect = () => ({ width: 348, height: 390 });
        Object.defineProperty(env.window, 'innerWidth', { configurable: true, value: 800 });
        Object.defineProperty(env.window, 'innerHeight', { configurable: true, value: 600 });

        env.eval('ContextMenuEmojiPicker.open("101", 750, 550)');
        await env.flush();

        expect(container.classList.contains('is-hidden')).toBe(false);
        expect(picker.classList.contains('dark')).toBe(true);
        expect(picker.classList.contains('light')).toBe(false);
        expect(container.style.left).toBe(`${800 - 348 - 8}px`);
        expect(container.style.top).toBe(`${600 - 390 - 8}px`);

        picker.dispatchEvent(new env.window.CustomEvent('emoji-click', {
            bubbles: true,
            detail: { unicode: '🔥' },
        }));
        await env.tick(20);

        expect(container.classList.contains('is-hidden')).toBe(true);
        const [reactPost] = env.xhrs('/chat/message/101/react');
        expect(reactPost).toBeDefined();
        expect(reactPost.method).toBe('POST');
        expect(new URLSearchParams(reactPost.body).get('emoji')).toBe('🔥');
    });

    it('closes on outside click, Escape, and when a message context menu opens', async () => {
        const env = await chatEnv();
        const container = env.$('#context-menu-emoji-picker');

        env.eval('ContextMenuEmojiPicker.open("101", 40, 40)');
        expect(container.classList.contains('is-hidden')).toBe(false);

        env.document.body.dispatchEvent(new env.window.MouseEvent('click', { bubbles: true }));
        expect(container.classList.contains('is-hidden')).toBe(true);

        env.eval('ContextMenuEmojiPicker.open("101", 40, 40)');
        env.document.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(container.classList.contains('is-hidden')).toBe(true);

        env.eval('ContextMenuEmojiPicker.open("101", 40, 40)');
        rightClick(env, env.$('#message-102'), 50, 50);
        await env.flush();
        expect(container.classList.contains('is-hidden')).toBe(true);
    });
});

describe('emoji-picker theme sync and search autofocus', () => {
    it('syncs all emoji-pickers when data-bs-theme toggles between dark and light', async () => {
        const env = await chatEnv();
        const composerPicker = env.$('#emoji-picker-container emoji-picker');
        const ctxPicker = env.$('#context-menu-emoji-picker emoji-picker');

        env.document.documentElement.setAttribute('data-bs-theme', 'dark');
        await env.flush();

        expect(composerPicker.classList.contains('dark')).toBe(true);
        expect(composerPicker.classList.contains('light')).toBe(false);
        expect(ctxPicker.classList.contains('dark')).toBe(true);
        expect(ctxPicker.classList.contains('light')).toBe(false);

        env.document.documentElement.setAttribute('data-bs-theme', 'light');
        await env.flush();

        expect(composerPicker.classList.contains('light')).toBe(true);
        expect(composerPicker.classList.contains('dark')).toBe(false);
        expect(ctxPicker.classList.contains('light')).toBe(true);
        expect(ctxPicker.classList.contains('dark')).toBe(false);
    });

    it('syncs theme and focuses input.search in shadowRoot when opening #emoji-btn', async () => {
        const env = await chatEnv();
        env.document.documentElement.setAttribute('data-bs-theme', 'dark');
        await env.flush();

        const picker = env.$('#emoji-picker-container emoji-picker');
        const shadow = picker.attachShadow({ mode: 'open' });
        const searchInput = env.document.createElement('input');
        searchInput.className = 'search';
        const focusSpy = vi.spyOn(searchInput, 'focus');
        shadow.appendChild(searchInput);

        env.window.requestAnimationFrame = (cb) => cb();
        env.$('#emoji-btn').click();

        expect(isShown(env.$('#emoji-picker-container'))).toBe(true);
        expect(picker.classList.contains('dark')).toBe(true);
        expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    });

    it('patches data-source and syncs theme on dynamically inserted emoji-pickers', async () => {
        const env = await chatEnv();
        env.document.documentElement.setAttribute('data-bs-theme', 'dark');
        await env.flush();

        const dynamicPicker = env.document.createElement('emoji-picker');
        env.$('#message-list').appendChild(dynamicPicker);
        await env.flush();

        expect(dynamicPicker.getAttribute('data-source')).toBe('/js/local/emoji.json');
        expect(dynamicPicker.classList.contains('dark')).toBe(true);
    });
});

describe('composer autofocus on channel switch', () => {
    it('focuses the WYSIWYG editor after swapping #chat-messages-container', async () => {
        const pane2 = conversationPane({ convIdStr: 'channel_2', dbId: 2, rows: MSG_2 });
        const env = await chatEnv({
            wysiwyg: true,
            routes: [{ match: '/chat/channel/2', reply: { body: pane2 } }],
        });

        const editor = env.$('#wysiwyg-editor');
        const focusSpy = vi.spyOn(editor, 'focus');

        env.eval(`htmx.ajax('GET', '/chat/channel/2', { target: '#chat-messages-container', swap: 'innerHTML' })`);
        await env.tick(50);

        expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    });

    it('focuses the markdown textarea when markdown mode is active', async () => {
        const pane2 = conversationPane({ convIdStr: 'channel_2', dbId: 2, rows: MSG_2 });
        const env = await chatEnv({
            wysiwyg: false,
            routes: [{ match: '/chat/channel/2', reply: { body: pane2 } }],
        });

        const markdown = env.$('#markdown-toggle-view');
        const focusSpy = vi.spyOn(markdown, 'focus');

        env.eval(`htmx.ajax('GET', '/chat/channel/2', { target: '#chat-messages-container', swap: 'innerHTML' })`);
        await env.tick(50);

        expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    });
});
