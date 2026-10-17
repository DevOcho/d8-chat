// The composer (createEditor): loaded the real way — htmx fetches
// /chat/input/default, swaps in chat_input_default.html, and its inline script
// dispatches `initializeEditor`. Covers mention autocomplete insertion (rich
// and markdown modes) and sending over HTTP POST.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    ME, chatInputDefault, chatPage, conversationPane, mentionResults, preferenceReply, preferenceSelects,
} from './helpers/fixtures.js';

const JOSUE = { id: 7, username: 'josue_sahad', displayName: 'Josue' };
const JOSE = { id: 8, username: 'jose', displayName: 'Jose' };

async function composerEnv({ wysiwyg = true, pane = conversationPane({ convIdStr: 'channel_1' }), ...opts } = {}) {
    const env = await createPage({
        html: chatPage({ pane, inputLoader: true, user: { ...ME } }),
        xhrRoutes: [
            { match: '/chat/input/default', reply: { body: chatInputDefault({ wysiwyg }) } },
            { match: '/chat/conversation/channel_1/mention_search', reply: { body: mentionResults([JOSUE, JOSE]) } },
        ],
        ...opts,
    });
    await env.openSocket();
    await env.tick(50);
    env.editor = env.$('#wysiwyg-editor');
    env.markdown = env.$('#markdown-toggle-view');
    env.hidden = env.$('#chat-message-input');
    return env;
}

// Put text in the rich editor and the caret at `caret` within `textNode`.
function setCaret(env, textNode, caret) {
    const range = env.document.createRange();
    range.setStart(textNode, caret);
    range.collapse(true);
    const sel = env.window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
}

async function typeRich(env, html) {
    env.editor.innerHTML = html;
    // Caret at the end of the last text node.
    const walker = env.document.createTreeWalker(env.editor, env.window.NodeFilter.SHOW_TEXT);
    let last = null;
    while (walker.nextNode()) last = walker.currentNode;
    setCaret(env, last, last.textContent.length);
    env.editor.dispatchEvent(new env.window.Event('input', { bubbles: true }));
    await env.flush();
}

async function typeMarkdown(env, text) {
    env.markdown.value = text;
    env.markdown.setSelectionRange(text.length, text.length);
    env.markdown.dispatchEvent(new env.window.Event('input', { bubbles: true }));
    await env.flush();
}

function key(env, target, k, extra = {}) {
    const evt = new env.window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra });
    target.dispatchEvent(evt);
    return evt;
}

// The mention popover createMentionManager appends to the form (the emoji one
// carries .emoji-suggestion-popover).
const popover = (env) => env.$('#message-form > div.shadow-sm:not(.emoji-suggestion-popover)');

// Known bug, see "picking a mention with the mouse does not throw" below.
const DETACHED_BUTTON_ERROR = /reading 'htmx-internal-data'/;

async function pick(env, username) {
    env.ignoreErrors(DETACHED_BUTTON_ERROR);
    env.$(`.mention-suggestion-item[data-username="${username}"]`).click();
    await env.flush();
}
const messagePosts = (env) => env.xhrs(/\/messages$/).filter((x) => x.method === 'POST');

describe('composer initialization', () => {
    it('initializes the editor swapped in by htmx', async () => {
        const env = await composerEnv();
        expect(env.$('#send-button').title).toBe('Send (Enter)');
        expect(env.editor.style.display).toBe('block');
        expect(env.markdown.style.display).toBe('none');
    });

    it('shows Ctrl+Enter on the send button when the user prefers it', async () => {
        const env = await createPage({
            html: chatPage({ pane: conversationPane(), inputLoader: true, user: { ...ME, sendOnEnter: false } }),
            xhrRoutes: [{ match: '/chat/input/default', reply: { body: chatInputDefault() } }],
        });
        await env.tick(50);
        expect(env.$('#send-button').title).toBe('Send (Ctrl+Enter)');
    });

    it('keeps an edit composer\'s own label (data-send-label) instead of "Send"', async () => {
        const editInput = chatInputDefault().replace(
            'class="btn btn-primary send-btn-dynamic"></button>',
            'class="btn btn-primary send-btn-dynamic" data-send-label="Save"></button>',
        );
        const env = await createPage({
            html: chatPage({ pane: conversationPane(), inputLoader: true, user: { ...ME } }),
            xhrRoutes: [{ match: '/chat/input/default', reply: { body: editInput } }],
        });
        await env.tick(50);
        expect(env.$('#send-button').title).toBe('Save (Enter)');
        expect(env.$('#send-button').textContent).toContain('Save');
    });
});

describe('mention autocomplete (rich editor)', () => {
    it('searches the open conversation for the text after "@"', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hey @jo');
        expect(env.xhrs(/mention_search/).map((x) => x.url)).toEqual([
            '/chat/conversation/channel_1/mention_search?q=jo',
        ]);
        await env.flush();
        expect(popover(env).style.display).toBe('block');
        expect(env.$$('.mention-suggestion-item').map((b) => b.dataset.username)).toEqual(['josue_sahad', 'jose']);
    });

    it('does not trigger inside a word (an email address)', async () => {
        const env = await composerEnv();
        await typeRich(env, 'mail kp@devo');
        expect(env.xhrs(/mention_search/)).toEqual([]);
    });

    it('replaces the typed "@query" with a mention pill (click)', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hey @jo');
        await env.flush();
        await pick(env, 'josue_sahad');

        // Regression (1f1d86d): the typed "@jo" was left next to the mention.
        expect(env.editor.textContent).toBe('hey @josue_sahad\u00A0');
        const pill = env.editor.querySelector('.mention-token');
        expect(pill.textContent).toBe('@josue_sahad');
        expect(pill.getAttribute('contenteditable')).toBe('false');
        expect(popover(env).style.display).toBe('none');
    });

    it('serializes the pill verbatim, without escaping underscores in the username', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hey @jo');
        await env.flush();
        await pick(env, 'josue_sahad');
        // Regression (1f1d86d): turndown produced "@josue\_sahad", which the
        // server's mention parser doesn't recognize.
        expect(env.hidden.value).toBe('hey @josue_sahad');
    });

    it('only replaces the query inside the text node holding the caret', async () => {
        const env = await composerEnv();
        await typeRich(env, '<b>bold</b> then @jos');
        expect(env.xhrs(/mention_search/).at(-1).url).toBe('/chat/conversation/channel_1/mention_search?q=jos');
        await env.flush();
        await pick(env, 'jose');
        expect(env.editor.innerHTML).toBe('<b>bold</b> then <span class="mention-token" contenteditable="false">@jose</span>&nbsp;');
    });

    it('moves the selection with the arrow keys and picks with Tab', async () => {
        const env = await composerEnv();
        await typeRich(env, '@jo');
        await env.flush();
        key(env, env.editor, 'ArrowDown');
        expect(env.$('.mention-suggestion-item.active').dataset.username).toBe('jose');
        key(env, env.editor, 'Tab');
        await env.flush();
        expect(env.editor.querySelector('.mention-token').textContent).toBe('@jose');
    });

    it('closes on Escape without inserting anything', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hi @jo');
        await env.flush();
        key(env, env.editor, 'Escape');
        expect(popover(env).style.display).toBe('none');
        expect(env.editor.textContent).toBe('hi @jo');
    });

    // Regression (fixed): clicking a suggestion runs the popover's click handler, which
    // empties the popover (detaching the clicked <button>) while the click is
    // still bubbling. htmx's submitter-tracking click listener on the
    // enclosing hx-post form then looks up the detached button's form, gets
    // null, and throws "Cannot read properties of null (reading
    // 'htmx-internal-data')" — an uncaught error on every mouse-picked mention.
    it('picking a mention with the mouse does not throw', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hey @jo');
        await env.flush();
        env.allowErrors();
        env.$('.mention-suggestion-item[data-username="jose"]').click();
        await env.flush();
        expect(env.editor.querySelector('.mention-token').textContent).toBe('@jose');
        expect(env.state.errors.map((e) => e.message)).toEqual([]);
    });

    // Regression (fixed): the mention keydown handler picks the suggestion and closes the
    // popover with stopPropagation() only. The editor's own Enter handler is
    // on the same element, runs next, sees the popover already closed, and
    // calls requestSubmit(): choosing a mention with Enter sends the
    // half-written message. (The emoji popover uses stopImmediatePropagation()
    // for exactly this reason.)
    it('choosing a mention with Enter inserts it without sending the message', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hey @jo');
        await env.flush();
        key(env, env.editor, 'Enter');
        await env.flush();
        expect(env.editor.querySelector('.mention-token').textContent).toBe('@josue_sahad');
        expect(messagePosts(env)).toEqual([]);
    });
});

describe('mention autocomplete (markdown mode)', () => {
    it('inserts "@username " at the caret and keeps the surrounding text', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, 'ping @jo');
        await env.flush();
        await pick(env, 'josue_sahad');
        expect(env.markdown.value).toBe('ping @josue_sahad ');
        expect(env.markdown.selectionStart).toBe('ping @josue_sahad '.length);
    });

    it('hides the popover once the query contains a space', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, 'ping @jo');
        await env.flush();
        await typeMarkdown(env, 'ping @jo hi');
        expect(popover(env).style.display).toBe('none');
    });
});

describe('sending', () => {
    it('POSTs the markdown of the composed message to the open conversation on Enter', async () => {
        const env = await composerEnv();
        await typeRich(env, 'ship <b>it</b>');
        key(env, env.editor, 'Enter');
        await env.flush();
        const [post] = messagePosts(env);
        expect(post.url).toBe('/chat/conversations/channel_1/messages');
        expect(new URLSearchParams(post.body).get('chat_message')).toBe('ship **it**');
        expect(post.requestHeaders['X-CSRFToken']).toBe('csrf-test-token');
    });

    it('Shift+Enter does not send', async () => {
        const env = await composerEnv();
        await typeRich(env, 'line one');
        key(env, env.editor, 'Enter', { shiftKey: true });
        await env.flush();
        expect(messagePosts(env)).toEqual([]);
    });

    it('does not send an empty message', async () => {
        const env = await composerEnv();
        await typeRich(env, '   ');
        key(env, env.editor, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toEqual([]);
    });

    it('clears the composer after the server accepts the message', async () => {
        const env = await composerEnv();
        await typeRich(env, 'hello');
        key(env, env.editor, 'Enter');
        await env.flush();
        messagePosts(env)[0].respond(204, '');
        await env.tick(50);
        expect(env.editor.innerHTML).toBe('');
        expect(env.hidden.value).toBe('');
        expect(env.toasts).toEqual([]);
    });

    it('keeps the draft and warns when the send fails', async () => {
        const env = await composerEnv();
        await typeRich(env, 'important');
        key(env, env.editor, 'Enter');
        await env.flush();
        messagePosts(env)[0].respond(500, 'boom');
        await env.tick(50);
        expect(env.editor.textContent).toBe('important');
        expect(env.toasts.map((t) => t.title)).toContain('Message not sent');
    });

    it('refuses to send when no conversation is open', async () => {
        const env = await composerEnv({ pane: '<div class="text-center text-muted">Pick a conversation</div>' });
        await typeRich(env, 'into the void');
        key(env, env.editor, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toEqual([]);
        expect(env.toasts.map((t) => t.title)).toEqual(['Not connected']);
    });
});

// A page with the Preferences selects (partials/profile_details.html) next to
// the composer, the routes answering as blueprints/profile.py does.
async function prefsEnv({ sendOnEnter = true, wysiwyg = true } = {}) {
    const env = await createPage({
        html: chatPage({
            pane: conversationPane({ convIdStr: 'channel_1' }),
            inputLoader: true,
            user: { ...ME, sendOnEnter },
            extraMain: preferenceSelects({ sendOnEnter }),
        }),
        xhrRoutes: [
            { match: '/chat/input/default', reply: { body: chatInputDefault({ wysiwyg }) } },
            {
                match: '/profile/send_on_enter',
                reply: (x) => preferenceReply(
                    'update-send-on-enter', new URLSearchParams(x.body).get('send_on_enter') === 'true',
                ),
            },
        ],
    });
    await env.openSocket();
    await env.tick(50);
    env.editor = env.$('#wysiwyg-editor');
    env.markdown = env.$('#markdown-toggle-view');
    return env;
}

// Pick an option the way a person does; htmx PUTs it on change.
async function choose(env, selector, value) {
    const select = env.$(selector);
    select.value = value;
    select.dispatchEvent(new env.window.Event('change', { bubbles: true }));
    await env.tick(20);
}

describe('send-key preference', () => {
    // Regression (fixed): the listener read evt.detail['update-send-on-enter'],
    // but htmx delivers a plain HX-Trigger value as evt.detail.value, so every
    // live change resolved to "Enter sends" and Ctrl+Enter needed a reload.
    it('switching to Ctrl+Enter applies without a reload', async () => {
        const env = await prefsEnv({ sendOnEnter: true });
        await typeRich(env, 'draft');
        await choose(env, '#send-key-select', 'false');
        expect(env.xhrs(/send_on_enter/).map((x) => x.method)).toEqual(['PUT']);
        expect(env.$('main.main-content').dataset.sendOnEnter).toBe('false');
        expect(env.$('#send-button').title).toBe('Send (Ctrl+Enter)');

        key(env, env.editor, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toEqual([]);
        key(env, env.editor, 'Enter', { ctrlKey: true });
        await env.flush();
        expect(messagePosts(env)).toHaveLength(1);
    });

    it('switching back to Enter-sends applies without a reload', async () => {
        const env = await prefsEnv({ sendOnEnter: false });
        await typeRich(env, 'draft');
        await choose(env, '#send-key-select', 'true');
        expect(env.$('main.main-content').dataset.sendOnEnter).toBe('true');
        expect(env.$('#send-button').title).toBe('Send (Enter)');

        key(env, env.editor, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toHaveLength(1);
    });
});

describe('lists (rich editor)', () => {
    // jsdom has no editing commands; record the one the editor asks for. The
    // real result (a new <li>, or leaving the list) is in tests/e2e.
    function recordCommands(env) {
        const commands = [];
        env.document.execCommand = (cmd) => {
            commands.push(cmd);
            return true;
        };
        return commands;
    }

    it('Shift+Enter in a bullet starts the next bullet instead of a line break (Enter sends)', async () => {
        const env = await composerEnv();
        await typeRich(env, '<ul><li>one</li></ul>');
        const commands = recordCommands(env);
        const evt = key(env, env.editor, 'Enter', { shiftKey: true });
        await env.flush();
        expect(evt.defaultPrevented).toBe(true);
        expect(commands).toEqual(['insertParagraph']);
        expect(messagePosts(env)).toEqual([]);
    });

    it('works in a numbered list too', async () => {
        const env = await composerEnv();
        await typeRich(env, '<ol><li>first</li></ol>');
        const commands = recordCommands(env);
        key(env, env.editor, 'Enter', { shiftKey: true });
        expect(commands).toEqual(['insertParagraph']);
    });

    it('Shift+Enter outside a list is still a plain line break', async () => {
        const env = await composerEnv();
        await typeRich(env, '<div>hello</div>');
        const commands = recordCommands(env);
        const evt = key(env, env.editor, 'Enter', { shiftKey: true });
        await env.flush();
        expect(evt.defaultPrevented).toBe(false);
        expect(commands).toEqual([]);
        expect(messagePosts(env)).toEqual([]);
    });

    it('plain Enter in a bullet still sends (Enter sends)', async () => {
        const env = await composerEnv();
        await typeRich(env, '<ul><li>one</li></ul>');
        key(env, env.editor, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toHaveLength(1);
    });

    it('with Ctrl+Enter sending, plain Enter in a bullet is left to the browser (it adds the bullet)', async () => {
        const env = await prefsEnv({ sendOnEnter: false });
        await typeRich(env, '<ul><li>one</li></ul>');
        const commands = recordCommands(env);
        const evt = key(env, env.editor, 'Enter');
        await env.flush();
        expect(evt.defaultPrevented).toBe(false);
        expect(commands).toEqual([]);
        expect(messagePosts(env)).toEqual([]);
    });
});

describe('lists (markdown view)', () => {
    async function shiftEnter(env) {
        const evt = key(env, env.markdown, 'Enter', { shiftKey: true });
        await env.flush();
        return evt;
    }

    it('Shift+Enter on a bullet line starts the next bullet (Enter sends)', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '- one');
        const evt = await shiftEnter(env);
        expect(evt.defaultPrevented).toBe(true);
        expect(env.markdown.value).toBe('- one\n- ');
        expect(env.markdown.selectionStart).toBe('- one\n- '.length);
        expect(messagePosts(env)).toEqual([]);
    });

    it('keeps the marker and indentation of the item', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '* one\n  + two');
        await shiftEnter(env);
        expect(env.markdown.value).toBe('* one\n  + two\n  + ');
    });

    it('numbers the next item of a numbered list', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '1. one\n2. two');
        await shiftEnter(env);
        expect(env.markdown.value).toBe('1. one\n2. two\n3. ');
    });

    it('an empty item ends the list', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '- one\n- ');
        const evt = await shiftEnter(env);
        expect(evt.defaultPrevented).toBe(true);
        expect(env.markdown.value).toBe('- one\n');
        expect(env.markdown.selectionStart).toBe('- one\n'.length);
    });

    it('splits an item at the caret', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '- onetwo');
        env.markdown.setSelectionRange(5, 5);
        await shiftEnter(env);
        expect(env.markdown.value).toBe('- one\n- two');
        expect(env.markdown.selectionStart).toBe('- one\n- '.length);
    });

    it('leaves Shift+Enter alone outside a list, and for markdown that only looks like one', async () => {
        for (const text of ['hello', '**bold**', '---', '-dash']) {
            const env = await composerEnv({ wysiwyg: false });
            await typeMarkdown(env, text);
            const evt = await shiftEnter(env);
            expect(evt.defaultPrevented, text).toBe(false);
            expect(env.markdown.value).toBe(text);
        }
    });

    it('plain Enter on a bullet line still sends (Enter sends)', async () => {
        const env = await composerEnv({ wysiwyg: false });
        await typeMarkdown(env, '- one');
        key(env, env.markdown, 'Enter');
        await env.flush();
        expect(messagePosts(env)).toHaveLength(1);
    });

    it('with Ctrl+Enter sending, plain Enter continues the list and Ctrl+Enter sends', async () => {
        const env = await prefsEnv({ sendOnEnter: false, wysiwyg: false });
        await typeMarkdown(env, '- one');
        const evt = key(env, env.markdown, 'Enter');
        await env.flush();
        expect(evt.defaultPrevented).toBe(true);
        expect(env.markdown.value).toBe('- one\n- ');
        expect(messagePosts(env)).toEqual([]);
        key(env, env.markdown, 'Enter', { ctrlKey: true });
        await env.flush();
        expect(messagePosts(env)).toHaveLength(1);
    });
});
