// Behaviors that partials ask for with data attributes instead of inline
// scripts, on* handlers or hx-on (none of which the page CSP allows): editor
// set-up for swapped-in composers, edit-composer after-save, copy, the
// new-message modal's focus flag, the create-poll form, autosubmit and the
// notification-sound preview.
import { describe, expect, it, vi } from 'vitest';
import { createPage } from './helpers/env.js';
import { ME, chatInputDefault, chatPage, conversationPane, message } from './helpers/fixtures.js';

const MINE = message({ id: 101, user: ME, createdAt: '2026-10-17T13:00:00', content: 'mine' });

async function chatEnv({ routes = [], extraMain = '' } = {}) {
    const env = await createPage({
        html: chatPage({ pane: conversationPane({ convIdStr: 'channel_1', rows: MINE }), inputLoader: true, extraMain }),
        xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault() } }, ...routes],
    });
    await env.openSocket();
    await env.tick(50);
    return env;
}

const swapIn = async (env, url, target) => {
    env.eval(`htmx.ajax('GET', ${JSON.stringify(url)}, { target: ${JSON.stringify(target)}, swap: 'outerHTML' })`);
    await env.tick(50);
};

// partials/chat_input_edit.html: the default composer's markup, but saving
// PUTs over the message and data-after-save leads back to the default one.
function editComposer({ wysiwyg = true } = {}) {
    return chatInputDefault({ wysiwyg })
        .replace(
            `<form hx-post="/chat/conversations/_pending_/messages"
          hx-swap="none"`,
            `<form hx-put="/chat/message/101"
          hx-target="#message-101"
          hx-swap="outerHTML"
          data-after-save="/chat/input/default"
          data-after-save-target="#chat-input-container"`,
        )
        .replace('class="btn btn-primary send-btn-dynamic"></button>', 'class="btn btn-primary send-btn-dynamic" data-send-label="Save"></button>')
        .replace('name="chat_message"', 'name="content"')
        .replace('id="chat-input-container"', 'id="chat-input-container" data-mode="edit"');
}

// partials/chat_input_thread.html, trimmed to what the editor needs.
function threadComposer(parentId) {
    const s = `-thread-${parentId}`;
    return `<div id="thread-view-${parentId}"><div id="thread-input-container-${parentId}" class="chat-input-form">
    <form hx-post="/chat/conversations/_pending_/messages" hx-swap="none" id="message-form${s}" data-editor="${s}" autocomplete="off">
        <input type="hidden" name="parent_message_id" value="${parentId}">
        <input type="file" id="file-attachment-input${s}" class="d-none" multiple>
        <input type="hidden" name="attachment_file_ids" id="attachment-file-ids${s}">
        <div class="wysiwyg-editor-container">
            <div id="attachment-previews${s}" class="p-2 flex-wrap gap-2"></div>
            <div class="wysiwyg-toolbar toolbar-hidden">
                <button type="button" data-command="formatBlock" data-value="blockquote" title="Quote"><i class="bi bi-quote"></i></button>
            </div>
            <div id="wysiwyg-editor${s}" class="wysiwyg-content" contenteditable="true"></div>
            <textarea id="markdown-toggle-view${s}" class="form-control" rows="1"></textarea>
            <textarea id="chat-message-input${s}" name="chat_message"></textarea>
            <div class="wysiwyg-toolbar wysiwyg-toolbar-bottom">
                <button type="button" id="format-toggle-btn${s}"><i class="bi bi-type"></i></button>
                <button type="button" id="file-attachment-btn${s}"><i class="bi bi-paperclip"></i></button>
                <button type="button" id="mention-btn${s}"><i class="bi bi-at"></i></button>
                <button type="button" id="emoji-btn${s}"><i class="bi bi-emoji-smile"></i></button>
                <button id="send-button${s}" type="submit" class="btn btn-primary send-btn-dynamic"></button>
            </div>
        </div>
        <div id="emoji-picker-container${s}"><emoji-picker class="light"></emoji-picker></div>
    </form>
</div></div>`;
}

describe('composers swapped in by htmx', () => {
    it('initialize their editor from the data-editor marker, with no inline script', async () => {
        const env = await chatEnv({
            routes: [{ match: '/chat/thread/101', reply: { body: threadComposer(101) } }],
            extraMain: '<div id="thread-slot"></div>',
        });
        expect(env.$('#message-form').dataset.editorReady).toBe('1');
        expect(env.$$('script:not([src])')).toHaveLength(0);

        await swapIn(env, '/chat/thread/101', '#thread-slot');
        expect(env.$('#message-form-thread-101').dataset.editorReady).toBe('1');
        // The editor is live: Enter sends what was typed, as a thread reply.
        // (Neither view is marked is-hidden in this fixture, so it starts in
        // the markdown view.)
        const input = env.$('#markdown-toggle-view-thread-101');

        input.value = 'in the thread';
        input.dispatchEvent(new env.window.Event('input', { bubbles: true }));
        input.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        await env.flush();
        const [post] = env.xhrs(/\/messages$/).filter((x) => x.method === 'POST');
        const body = new URLSearchParams(post.body);
        expect(body.get('chat_message')).toBe('in the thread');
        expect(body.get('parent_message_id')).toBe('101');
    });

    it('an edit composer goes back to the default one once the edit is saved', async () => {
        const env = await chatEnv({
            routes: [
                { match: '/chat/message/101/load_for_edit', reply: { body: editComposer() } },
                { match: (x) => x.method === 'PUT', reply: { body: MINE } },
            ],
        });
        await swapIn(env, '/chat/message/101/load_for_edit', '#chat-input-container');
        expect(env.$('#chat-input-container').dataset.mode).toBe('edit');

        const defaultLoads = env.xhrs('/chat/input/default').length;
        const editor = env.$('#wysiwyg-editor');
        editor.innerHTML = 'fixed <b>typo</b>';
        editor.dispatchEvent(new env.window.Event('input', { bubbles: true }));
        env.$('#send-button').click();
        await env.tick(50);
        const puts = env.xhrs(/\/chat\/message\/101$/);
        expect(puts.map((x) => x.method)).toEqual(['PUT']);
        expect(new URLSearchParams(puts[0].body).get('content')).toBe('fixed **typo**');
        expect(env.xhrs('/chat/input/default')).toHaveLength(defaultLoads + 1);
        expect(env.$('#chat-input-container').dataset.mode).toBeUndefined();
    });

    it('saves what was typed in the markdown view too', async () => {
        const env = await chatEnv({
            routes: [
                { match: '/chat/message/101/load_for_edit', reply: { body: editComposer({ wysiwyg: false }) } },
                { match: (x) => x.method === 'PUT', reply: { body: MINE } },
            ],
        });
        await swapIn(env, '/chat/message/101/load_for_edit', '#chat-input-container');
        const markdown = env.$('#markdown-toggle-view');
        markdown.value = 'fixed **typo**';
        markdown.dispatchEvent(new env.window.Event('input', { bubbles: true }));
        env.$('#send-button').click();
        await env.tick(50);
        const [put] = env.xhrs(/\/chat\/message\/101$/);
        expect(new URLSearchParams(put.body).get('content')).toBe('fixed **typo**');
    });

    it('a failed save stays in edit mode', async () => {
        const env = await chatEnv({
            routes: [
                { match: '/chat/message/101/load_for_edit', reply: { body: editComposer() } },
                { match: (x) => x.method === 'PUT', reply: { status: 500, body: 'nope' } },
            ],
        });
        env.ignoreErrors(/Response Status Error/);
        await swapIn(env, '/chat/message/101/load_for_edit', '#chat-input-container');
        const defaultLoads = env.xhrs('/chat/input/default').length;
        env.trigger(env.$('#message-form'), 'submit');
        await env.tick(50);
        expect(env.xhrs('/chat/input/default')).toHaveLength(defaultLoads);
        expect(env.$('#chat-input-container').dataset.mode).toBe('edit');
    });

    it('a request from a button inside the edit composer does not end edit mode', async () => {
        const env = await chatEnv({
            routes: [
                { match: '/chat/message/101/load_for_edit', reply: { body: editComposer() } },
                { match: '/chat/mentions', reply: { body: '<div></div>' } },
            ],
        });
        await swapIn(env, '/chat/message/101/load_for_edit', '#chat-input-container');
        const defaultLoads = env.xhrs('/chat/input/default').length;
        const inner = env.document.createElement('button');
        inner.type = 'button';
        inner.setAttribute('hx-get', '/chat/mentions');
        inner.setAttribute('hx-swap', 'none');
        env.$('#message-form').appendChild(inner);
        env.eval('htmx.process(document.getElementById("message-form"))');
        inner.click();
        await env.tick(50);
        expect(env.xhrs('/chat/mentions')).toHaveLength(1);
        expect(env.xhrs('/chat/input/default')).toHaveLength(defaultLoads);
    });
});

describe('copy button', () => {
    it('copies the message text and shows a check for two seconds', async () => {
        const env = await chatEnv();
        const writeText = vi.fn(() => Promise.resolve());
        Object.defineProperty(env.window.navigator, 'clipboard', { configurable: true, value: { writeText } });
        env.$('#message-list').insertAdjacentHTML('beforeend', `
            <button id="copy" data-copy-message><i class="bi bi-clipboard"></i>
                <input type="hidden" class="copy-source" value="**raw** text"></button>`);
        env.$('#copy').click();
        await env.flush();
        expect(writeText).toHaveBeenCalledWith('**raw** text');
        expect(env.$('#copy i').className).toBe('bi bi-check2');
        await env.tick(2000);
        expect(env.$('#copy i').className).toBe('bi bi-clipboard');
    });
});

describe('new-message modal', () => {
    it('asks for the composer to be focused once the modal closes', async () => {
        const env = await chatEnv();
        env.$('#message-list').insertAdjacentHTML('beforeend', '<a href="#" id="pick" data-focus-composer>Robert</a>');
        expect(env.window.focusChatInputAfterModalClose).toBeFalsy();
        env.$('#pick').dispatchEvent(new env.window.MouseEvent('click', { bubbles: true, cancelable: true }));
        expect(env.window.focusChatInputAfterModalClose).toBe(true);
    });
});

// partials/create_poll_modal.html as the server renders it.
function pollForm(values = ['', ''], max = 10) {
    const row = (v, n) => `<div class="input-group mb-2 poll-option-row">
        <input type="text" class="form-control" name="options[]" placeholder="Option ${n}" value="${v}" required>
        <button class="btn btn-outline-danger remove-poll-option-btn" type="button" title="Remove option"><i class="bi bi-x-lg"></i></button>
    </div>`;
    return `<form id="create-poll-form" data-max-options="${max}" hx-post="/chat/poll/create" hx-target="this" hx-swap="outerHTML">
        <input type="hidden" name="conversation_id_str" value="" id="poll-conversation-id-input">
        <div id="poll-options-container">${values.map((v, i) => row(v, i + 1)).join('')}</div>
        <template id="poll-option-template">${row('', 1)}</template>
        <button type="button" id="add-poll-option-btn" data-poll-add-option class="btn btn-sm btn-secondary mt-2">Add Option</button>
    </form>`;
}

describe('create-poll form', () => {
    const open = async (body) => {
        const env = await chatEnv({
            routes: [{ match: '/chat/poll/create_form', reply: { body } }],
            extraMain: '<div id="poll-slot"></div>',
        });
        await swapIn(env, '/chat/poll/create_form', '#poll-slot');
        return env;
    };
    const placeholders = (env) => env.$$('#poll-options-container input').map((i) => i.placeholder);

    it('posts to the open conversation', async () => {
        const env = await open(pollForm());
        expect(env.$('#poll-conversation-id-input').value).toBe('channel_1');
    });

    it('adds rows up to the limit, then hides "Add option"', async () => {
        const env = await open(pollForm(['', ''], 4));
        const add = env.$('#add-poll-option-btn');
        add.click();
        expect(placeholders(env)).toEqual(['Option 1', 'Option 2', 'Option 3']);
        expect(env.document.activeElement).toBe(env.$$('#poll-options-container input')[2]);
        expect(add.classList.contains('d-none')).toBe(false);
        add.click();
        expect(placeholders(env)).toHaveLength(4);
        expect(add.classList.contains('d-none')).toBe(true);
    });

    it('removes a row, renumbers the rest and shows "Add option" again', async () => {
        const env = await open(pollForm(['Tacos', 'Pizza', 'Sushi'], 3));
        expect(env.$('#add-poll-option-btn').classList.contains('d-none')).toBe(true);
        env.$$('.remove-poll-option-btn')[0].click();
        expect(env.$$('#poll-options-container input').map((i) => i.value)).toEqual(['Pizza', 'Sushi']);
        expect(placeholders(env)).toEqual(['Option 1', 'Option 2']);
        expect(env.$('#add-poll-option-btn').classList.contains('d-none')).toBe(false);
    });
});

describe('data-autosubmit', () => {
    it('submits the htmx form when the field changes (avatar upload)', async () => {
        const env = await chatEnv({
            routes: [{ match: '/profile/avatar', reply: { body: '' } }],
            extraMain: `<form id="avatar-form" hx-post="/profile/avatar" hx-swap="none">
                <input type="text" name="avatar" id="avatar-input" data-autosubmit></form>`,
        });
        env.$('#avatar-input').dispatchEvent(new env.window.Event('change', { bubbles: true }));
        await env.tick(10);
        expect(env.xhrs('/profile/avatar').map((x) => x.method)).toEqual(['POST']);
    });
});

describe('notification-sound select', () => {
    it('plays the sound it is set to', async () => {
        const env = await chatEnv({
            extraMain: `<select id="sound" data-preview-sound>
                <option value="d8-notification.mp3">D8</option>
                <option value="slack-notification.mp3">Slack</option></select>`,
        });
        const select = env.$('#sound');
        select.value = 'slack-notification.mp3';
        select.dispatchEvent(new env.window.Event('change', { bubbles: true }));
        const audio = env.audios[env.audios.length - 1];
        expect(audio.src).toBe('/audio/slack-notification.mp3');
        expect(audio.played).toBe(true);
    });
});

describe('htmx config (base.html meta)', () => {
    it('runs no script or hx-on from swapped-in content, and injects no <style>', async () => {
        const env = await chatEnv({
            routes: [{
                match: '/chat/evil',
                reply: { body: '<div id="evil"><script>window.ranScript = true;</script><button id="evil-btn" hx-on:click="window.ranHxOn = true">x</button></div>' },
            }],
            extraMain: '<div id="evil-slot"></div>',
        });
        expect(env.window.htmx.config.allowEval).toBe(false);
        expect(env.window.htmx.config.allowScriptTags).toBe(false);
        expect(env.$$('head style')).toHaveLength(0);

        await swapIn(env, '/chat/evil', '#evil-slot');
        env.$('#evil-btn').click();
        expect(env.window.ranScript).toBeUndefined();
        expect(env.window.ranHxOn).toBeUndefined();
    });
});

describe('sizes from data attributes (ui.js)', () => {
    it('gives poll bars their width when htmx loads them, clamped to 0-100%', async () => {
        const env = await chatEnv({
            routes: [{
                match: '/chat/poll/1/results',
                reply: { body: '<div id="bars"><div class="poll-progress" data-width-percent="42.5"></div><div class="poll-progress" data-width-percent="140"></div></div>' },
            }],
            extraMain: '<div id="bars"></div>',
        });
        await swapIn(env, '/chat/poll/1/results', '#bars');
        const widths = env.$$('#bars .poll-progress').map((el) => el.style.width);
        expect(widths).toEqual(['42.5%', '100%']);
        expect(env.$$('#bars .poll-progress').every((el) => el.style.transition === '')).toBe(true);
    });
});

describe('elements hidden by class until chat.js shows them', () => {
    it('the emoji picker opens on the first click and closes on the second', async () => {
        const env = await chatEnv();
        const container = env.$('#emoji-picker-container');
        expect(container.classList.contains('is-hidden')).toBe(true);
        env.$('#emoji-btn').click();
        expect(container.style.display).toBe('block');
        env.$('#emoji-btn').click();
        expect(container.style.display).toBe('none');
    });

    it('a composer marked for markdown starts in the markdown view', async () => {
        const env = await createPage({
            html: chatPage({ pane: conversationPane({ convIdStr: 'channel_1', rows: MINE }), inputLoader: true }),
            xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault({ wysiwyg: false }) } }],
        });
        await env.tick(50);
        const markdown = env.$('#markdown-toggle-view');
        markdown.value = 'from the **markdown** view';
        markdown.dispatchEvent(new env.window.Event('input', { bubbles: true }));
        env.$('#send-button').click();
        await env.flush();
        const [post] = env.xhrs(/\/messages$/).filter((x) => x.method === 'POST');
        expect(new URLSearchParams(post.body).get('chat_message')).toBe('from the **markdown** view');
    });
});

describe('htmx settling', () => {
    it('never copies a style attribute onto swapped-in content', async () => {
        const env = await chatEnv({
            routes: [{ match: '/chat/box', reply: { body: '<div id="box" class="b"></div>' } }],
            extraMain: '<div id="box" class="a"></div>',
        });
        expect(env.window.htmx.config.attributesToSettle).toEqual(['class', 'width', 'height']);
        const old = env.$('#box');
        old.style.height = '120px'; // what chat.js does through the CSSOM
        const writes = [];
        const setAttribute = env.window.Element.prototype.setAttribute;
        env.window.Element.prototype.setAttribute = function (name, value) {
            if (name === 'style') writes.push(value);
            return setAttribute.call(this, name, value);
        };
        await swapIn(env, '/chat/box', '#box');
        env.window.Element.prototype.setAttribute = setAttribute;
        expect(writes).toEqual([]);
        expect(env.$('#box').className).toBe('b');
    });
});
