// Loads the real app scripts into a fresh jsdom window per test.
//
// The scripts are classic browser scripts (top-level const/let/function,
// DOMContentLoaded closures), so they are run with vm.Script in the window's
// own VM context: that gives true classic-script semantics (top-level consts
// such as NotificationManager become global lexical bindings visible to later
// scripts and to env.eval()) and real file names in stack traces.
//
// Timing mirrors chat.html: the markup is fully parsed, then the scripts run
// in page order (htmx, htmx-ws, turndown, the emoji-picker element, chat.js,
// pager.js — chat.js/pager.js are `defer`), then DOMContentLoaded fires. htmx
// therefore initializes first, exactly as in the browser.
//
// Every window gets fake timers (@sinonjs/fake-timers bound to that window, so
// the app's own setTimeout/setInterval/Date are the fake ones), a fake
// WebSocket/XHR/fetch, and stubs for Notification, Audio, AudioContext and
// Bootstrap. Uncaught exceptions in app code fail the test unless the test
// opts in with env.allowErrors().
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import FakeTimers from '@sinonjs/fake-timers';
import { JSDOM, VirtualConsole } from 'jsdom';
import {
    cssEscape,
    makeAudio,
    makeAudioContext,
    makeBootstrap,
    makeFakeWebSocket,
    makeFakeXHR,
    makeFetch,
    makeNotification,
} from './fakes.js';

export const ROOT = path.resolve(import.meta.dirname, '../../..');
export const BASE_URL = 'https://d8-chat.local/chat';
// A Saturday afternoon in October, UTC.
export const DEFAULT_NOW = '2026-10-17T14:00:00.000Z';

const SCRIPT_FILES = {
    htmx: 'app/static/js/local/htmx.min.js',
    'htmx-ws': 'app/static/js/local/htmx-ws.js',
    turndown: 'app/static/js/local/turndown.js',
    csrf: 'app/static/js/csrf.js',
    chat: 'app/static/js/chat.js',
    pager: 'app/static/js/pager.js',
};

// <emoji-picker> ships as an ES module bundle; chat.js only needs the element
// to be defined (it awaits customElements.whenDefined('emoji-picker')).
const EMOJI_PICKER_STUB = "customElements.define('emoji-picker', class extends HTMLElement {});";

export const PAGE_SCRIPTS = ['htmx', 'htmx-ws', 'turndown', 'csrf', 'emoji-picker', 'chat', 'pager'];

const compiled = new Map();
function script(name) {
    if (!compiled.has(name)) {
        if (name === 'emoji-picker') {
            compiled.set(name, { source: EMOJI_PICKER_STUB, filename: 'emoji-picker-stub.js' });
        } else {
            const filename = path.join(ROOT, SCRIPT_FILES[name]);
            compiled.set(name, { source: fs.readFileSync(filename, 'utf8'), filename });
        }
    }
    const { source, filename } = compiled.get(name);
    return new vm.Script(source, { filename });
}

export function readSource(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// Small emoji dataset in the shape of app/static/js/local/emoji.json.
export const EMOJI_DATA = [
    { emoji: '😄', shortcodes: ['smile'], order: 1 },
    { emoji: '😂', shortcodes: ['joy'], order: 2 },
    { emoji: '👍', shortcodes: ['thumbsup', '+1'], order: 3 },
];

const openPages = new Set();

export async function closeAllPages() {
    const pages = [...openPages];
    openPages.clear();
    const failures = [];
    for (const page of pages) {
        try {
            page.close();
        } catch (err) {
            failures.push(err);
        }
    }
    if (failures.length) throw failures[0];
}

/**
 * Build a page and run the app scripts in it.
 *
 * @param {object} opts
 * @param {string} opts.html            full document markup (see fixtures.js)
 * @param {string[]} [opts.scripts]     which scripts to load, in order
 * @param {string} [opts.now]           fake clock start (ISO)
 * @param {boolean} [opts.focused]      document.hasFocus()
 * @param {boolean} [opts.visible]      document.visibilityState === 'visible'
 * @param {string} [opts.notificationPermission]
 * @param {boolean} [opts.autoplayBlocked] AudioContext stays suspended until the test unblocks it
 * @param {Array} [opts.fetchRoutes]    [{ match, reply }] checked before the defaults
 * @param {Array} [opts.xhrRoutes]      [{ match, reply }] auto-answered htmx requests
 * @param {Array<{id:number}>} [opts.activePages]  what GET /oncall/pages/active returns
 */
export async function createPage(opts = {}) {
    const {
        html,
        scripts = PAGE_SCRIPTS,
        now = DEFAULT_NOW,
        focused = false,
        visible = true,
        notificationPermission = 'granted',
        autoplayBlocked = false,
        fetchRoutes = [],
        xhrRoutes = [],
        activePages = [],
    } = opts;

    const state = {
        notifications: [],
        audios: [],
        audioContexts: [],
        toasts: [],
        sockets: [],
        xhrs: [],
        errors: [],
        navigations: [],
        console: [],
        notificationPermission,
        autoplayBlocked,
        focused,
        visible,
        activePages,
    };

    const virtualConsole = new VirtualConsole();
    virtualConsole.on('jsdomError', (err) => {
        if (err.type === 'not-implemented' && /navigation/i.test(err.message)) {
            state.navigations.push(err.message);
        } else {
            state.errors.push(err);
        }
    });
    for (const level of ['log', 'info', 'warn', 'error', 'debug']) {
        virtualConsole.on(level, (...args) => state.console.push({ level, args }));
    }

    const defaultFetchRoutes = [
        { match: '/oncall/pages/active', reply: () => ({ json: { incidents: state.activePages } }) },
        { match: '/js/local/emoji.json', reply: { json: EMOJI_DATA } },
        { match: '/profile/activity', reply: { status: 204 } },
    ];
    const fetch = makeFetch([...fetchRoutes, ...defaultFetchRoutes]);

    let clock;
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });

    const dom = new JSDOM(html, {
        url: BASE_URL,
        runScripts: 'dangerously',
        pretendToBeVisual: true,
        virtualConsole,
        beforeParse(window) {
            clock = FakeTimers.withGlobal(window).install({
                now: Date.parse(now),
                toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'],
            });

            window.WebSocket = makeFakeWebSocket(state.sockets);
            window.XMLHttpRequest = makeFakeXHR(state.xhrs, xhrRoutes, BASE_URL);
            window.fetch = fetch;
            window.Notification = makeNotification(state);
            window.Audio = makeAudio(state);
            window.AudioContext = makeAudioContext(state);
            window.bootstrap = makeBootstrap(state);
            window.CSS = { escape: cssEscape };

            // htmx calls XPathExpression.evaluate(node) with the optional result
            // type omitted (it defaults to ANY_TYPE in browsers); jsdom insists.
            const xpathEvaluate = window.XPathExpression.prototype.evaluate;
            window.XPathExpression.prototype.evaluate = function evaluate(node, type = 0, result = null) {
                return xpathEvaluate.call(this, node, type, result);
            };

            // Layout/editing APIs jsdom doesn't implement.
            window.Element.prototype.scrollIntoView = function scrollIntoView() {};
            window.Element.prototype.scrollTo = function scrollTo() {};
            if (!('innerText' in window.HTMLElement.prototype)) {
                Object.defineProperty(window.HTMLElement.prototype, 'innerText', {
                    configurable: true,
                    get() { return this.textContent; },
                    set(v) { this.textContent = v; },
                });
            }
            const doc = window.document;
            doc.execCommand = () => false;
            doc.queryCommandState = () => false;
            doc.hasFocus = () => state.focused;
            Object.defineProperty(doc, 'hidden', { configurable: true, get: () => !state.visible });
            Object.defineProperty(doc, 'visibilityState', {
                configurable: true,
                get: () => (state.visible ? 'visible' : 'hidden'),
            });
            window.focus = () => {};

            doc.addEventListener('DOMContentLoaded', () => resolveReady());
        },
    });

    const { window } = dom;
    const context = dom.getInternalVMContext();

    const env = {
        dom,
        window,
        document: window.document,
        clock,
        fetch,
        state,
        _allowErrors: false,
        _ignoredErrors: [],

        $: (sel) => window.document.querySelector(sel),
        $$: (sel) => [...window.document.querySelectorAll(sel)],

        /** Evaluate an expression in the page's global scope. */
        eval: (code) => new vm.Script(code).runInContext(context),

        /** Drain pending promise callbacks without moving the clock. */
        async flush() {
            for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
        },

        /** Advance the fake clock, running timers and promise callbacks. */
        async tick(ms) {
            await clock.tickAsync(ms);
            await env.flush();
        },

        get sockets() { return state.sockets; },
        get socket() { return state.sockets[state.sockets.length - 1]; },
        get notifications() { return state.notifications; },
        get audios() { return state.audios; },
        get toasts() { return state.toasts; },
        get reloads() { return state.navigations.length; },

        /** Requests htmx made, optionally filtered by path/regex. */
        xhrs(match) {
            return state.xhrs.filter((x) => !match
                || (match instanceof RegExp ? match.test(x.url) : x.path === match));
        },

        /** fetch() calls, optionally filtered by path/regex: [{ url, init }]. */
        fetches(match) {
            return fetch.mock.calls
                .map(([url, init = {}]) => ({ url: String(url), init }))
                .filter(({ url }) => !match
                    || (match instanceof RegExp ? match.test(url)
                        : new URL(url, BASE_URL).pathname === match));
        },

        /** Push a WebSocket frame through the live socket (htmx-ws dispatch). */
        async frame(data, socket = env.socket) {
            socket.receive(data);
            await env.flush();
        },

        /** Open the current socket and let wsOpen handlers settle. */
        async openSocket(socket = env.socket) {
            socket.open();
            await env.flush();
        },

        async setVisible(isVisible) {
            state.visible = isVisible;
            window.document.dispatchEvent(new window.Event('visibilitychange'));
            await env.flush();
        },

        setFocused(isFocused) {
            state.focused = isFocused;
        },

        /** Fire a custom DOM event the way htmx does (bubbling CustomEvent). */
        trigger(target, name, detail = {}) {
            const evt = new window.CustomEvent(name, { bubbles: true, cancelable: true, detail });
            return target.dispatchEvent(evt);
        },

        allowErrors() {
            env._allowErrors = true;
        },

        /** Drop uncaught errors matching `pattern` (a known, reported bug). */
        ignoreErrors(pattern) {
            env._ignoredErrors.push(pattern);
        },

        close() {
            openPages.delete(env);
            clock.uninstall();
            window.close();
            const errors = state.errors.filter((e) => !env._ignoredErrors.some((p) => p.test(e.message)));
            if (errors.length && !env._allowErrors) {
                const detail = errors
                    .map((e) => `${e.message}\n${e.cause?.stack || e.stack || ''}`)
                    .join('\n---\n');
                throw new Error(`Uncaught error(s) in page scripts:\n${detail}`);
            }
        },
    };
    openPages.add(env);

    for (const name of scripts) {
        script(name).runInContext(context);
    }

    await ready;
    await env.flush();
    return env;
}
