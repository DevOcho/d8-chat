// Browser API fakes installed into each jsdom window by env.js.
//
// jsdom has no fetch / Notification / AudioContext, its WebSocket and
// XMLHttpRequest would really hit the network, and Audio.play() is
// unimplemented. These fakes stand in for them and record what the app did so
// tests can assert on it. Everything is per-window: build a fresh set for each
// page so nothing leaks between tests.
import { vi } from 'vitest';

// --- WebSocket ---------------------------------------------------------------

// Mirrors the parts of the browser WebSocket htmx-ws.js touches: readyState
// (plus the OPEN/CLOSED constants it reads off the *instance*), on* handlers,
// addEventListener for 'message', send() and close().
//
// The test drives the "network": open(), receive(), serverClose(). A
// client-initiated close() only moves to CLOSING, like a browser waiting for
// the server's close frame; completeClose() delivers the close event (code
// 1005 by default, which is what browsers report when the server echoes a
// close frame without a status code).
export function makeFakeWebSocket(registry) {
    class FakeWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;

        constructor(url, protocols) {
            this.url = url;
            this.protocols = protocols;
            this.readyState = FakeWebSocket.CONNECTING;
            this.binaryType = 'blob';
            this.sent = [];
            this.listeners = {};
            this.onopen = null;
            this.onclose = null;
            this.onerror = null;
            this.onmessage = null;
            this.clientClose = null;
            registry.push(this);
        }

        get CONNECTING() { return 0; }
        get OPEN() { return 1; }
        get CLOSING() { return 2; }
        get CLOSED() { return 3; }

        addEventListener(type, fn) {
            (this.listeners[type] ||= []).push(fn);
        }

        removeEventListener(type, fn) {
            this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
        }

        _dispatch(type, event) {
            const evt = { type, target: this, ...event };
            const handler = this[`on${type}`];
            if (typeof handler === 'function') handler.call(this, evt);
            for (const fn of this.listeners[type] || []) fn.call(this, evt);
        }

        send(data) {
            if (this.readyState !== FakeWebSocket.OPEN) {
                throw new Error(`send() on a socket in readyState ${this.readyState}`);
            }
            this.sent.push(data);
        }

        close(code, reason) {
            if (this.readyState >= FakeWebSocket.CLOSING) return;
            this.readyState = FakeWebSocket.CLOSING;
            this.clientClose = { code, reason };
        }

        // --- test controls ---

        open() {
            this.readyState = FakeWebSocket.OPEN;
            this._dispatch('open', {});
        }

        receive(data) {
            const payload = typeof data === 'string' ? data : JSON.stringify(data);
            this._dispatch('message', { data: payload });
        }

        error() {
            this._dispatch('error', {});
        }

        serverClose(code, reason = '') {
            this.readyState = FakeWebSocket.CLOSED;
            this._dispatch('close', { code, reason, wasClean: code === 1000 });
        }

        completeClose(code) {
            const sent = this.clientClose || {};
            this.serverClose(code ?? sent.code ?? 1005, sent.reason || '');
        }

        get sentJson() {
            return this.sent.map((s) => JSON.parse(s));
        }
    }
    return FakeWebSocket;
}

// --- XMLHttpRequest (htmx's transport) ---------------------------------------

// Just enough XHR for htmx 2: open/send/setRequestHeader/overrideMimeType, the
// on* handlers, addEventListener on the xhr and xhr.upload, and the response
// accessors htmx reads. Requests matching a route in `routes` are answered on
// a microtask; anything else stays pending until the test calls respond().
export function makeFakeXHR(registry, routes, baseUrl) {
    class Emitter {
        constructor() { this.listeners = {}; }
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
        removeEventListener() {}
        _emit(type, evt = {}) {
            for (const fn of this.listeners[type] || []) fn({ type, ...evt });
        }
    }

    class FakeXHR extends Emitter {
        constructor() {
            super();
            this.upload = new Emitter();
            this.readyState = 0;
            this.status = 0;
            this.response = '';
            this.responseText = '';
            this.responseURL = '';
            this.requestHeaders = {};
            this.responseHeaders = {};
            this.withCredentials = false;
            this.timeout = 0;
            this.done = false;
        }

        open(method, url) {
            this.method = method.toUpperCase();
            this.url = url;
            this.path = new URL(url, baseUrl).pathname;
            this.readyState = 1;
        }

        overrideMimeType() {}

        setRequestHeader(name, value) {
            this.requestHeaders[name] = value;
        }

        send(body) {
            this.body = body;
            registry.push(this);
            const route = routes.find((r) => matches(r.match, this));
            if (route) {
                Promise.resolve().then(() => {
                    if (this.done) return;
                    const res = typeof route.reply === 'function' ? route.reply(this) : route.reply;
                    this.respond(res.status ?? 200, res.body ?? '', res.headers ?? {});
                });
            }
        }

        abort() {
            this.done = true;
            if (this.onabort) this.onabort();
        }

        respond(status = 200, body = '', headers = {}) {
            if (this.done) throw new Error(`XHR ${this.method} ${this.url} already answered`);
            this.done = true;
            this.status = status;
            this.response = body;
            this.responseText = body;
            this.responseURL = new URL(this.url, baseUrl).href;
            this.responseHeaders = Object.fromEntries(
                Object.entries(headers).map(([k, v]) => [k.toLowerCase(), String(v)]),
            );
            this.readyState = 4;
            if (this.onload) this.onload();
            this._emit('loadend');
        }

        fail() {
            this.done = true;
            this.readyState = 4;
            if (this.onerror) this.onerror();
            this._emit('loadend');
        }

        getResponseHeader(name) {
            const v = this.responseHeaders[String(name).toLowerCase()];
            return v === undefined ? null : v;
        }

        getAllResponseHeaders() {
            return Object.entries(this.responseHeaders)
                .map(([k, v]) => `${k}: ${v}\r\n`).join('');
        }
    }
    return FakeXHR;
}

function matches(match, req) {
    if (typeof match === 'function') return match(req);
    if (match instanceof RegExp) return match.test(req.url);
    return req.path === match || req.url === match;
}

// --- fetch -------------------------------------------------------------------

// A vi.fn() fetch backed by a small router. Handlers return a plain object
// ({ status, json, body }) that becomes a minimal Response-like value.
export function makeFetch(routes) {
    return vi.fn(async (input, init = {}) => {
        const url = String(input);
        const path = new URL(url, 'https://d8-chat.local').pathname;
        const req = { url, path, method: (init.method || 'GET').toUpperCase(), init };
        const route = routes.find((r) => (typeof r.match === 'function'
            ? r.match(req)
            : r.match instanceof RegExp ? r.match.test(url) : r.match === path));
        if (!route) return response({ status: 404, json: { error: 'not found' } });
        const res = typeof route.reply === 'function' ? await route.reply(req) : route.reply;
        if (res instanceof Error) throw res;
        return response(res);
    });
}

export function response({ status = 200, json, body } = {}) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => {
            if (json === undefined) throw new SyntaxError('Unexpected end of JSON input');
            return json;
        },
        text: async () => body ?? (json !== undefined ? JSON.stringify(json) : ''),
    };
}

// A fetch reply that stays pending until the test resolves it — for races.
export function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

// --- Notification / Audio / AudioContext --------------------------------------

export function makeNotification(state) {
    class FakeNotification {
        static get permission() { return state.notificationPermission; }
        static requestPermission = vi.fn(async () => state.notificationPermission);

        constructor(title, options = {}) {
            this.title = title;
            this.options = options;
            this.closed = false;
            this.onclick = null;
            state.notifications.push(this);
        }

        close() { this.closed = true; }
    }
    return FakeNotification;
}

export function makeAudio(state) {
    return class FakeAudio {
        constructor(src) {
            this.src = src;
            this.played = false;
            state.audios.push(this);
        }

        play() {
            this.played = true;
            return Promise.resolve();
        }

        pause() {}
    };
}

// Web Audio graph stub. Records oscillators so tests can tell whether the
// pager's siren is running (started and not yet stopped).
export function makeAudioContext(state) {
    const node = (extra = {}) => ({
        connect: vi.fn((dest) => dest),
        disconnect: vi.fn(),
        ...extra,
    });
    return class FakeAudioContext {
        constructor() {
            // Browsers start a context 'suspended' until the page has had a
            // user gesture (autoplay policy).
            this.state = state.autoplayBlocked ? 'suspended' : 'running';
            this.destination = {};
            this.listeners = {};
            this.oscillators = [];
            state.audioContexts.push(this);
        }

        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }

        // While autoplay is blocked, resume() stays pending (as in Chrome
        // without a user activation). Tests unblock it to model a gesture.
        resume() {
            if (state.autoplayBlocked) return new Promise(() => {});
            if (this.state !== 'running') {
                this.state = 'running';
                for (const fn of this.listeners.statechange || []) fn();
            }
            return Promise.resolve();
        }

        createOscillator() {
            const osc = node({
                type: 'sine',
                frequency: { value: 440 },
                started: false,
                stopped: false,
            });
            osc.start = vi.fn(() => { osc.started = true; });
            osc.stop = vi.fn(() => { osc.stopped = true; });
            this.oscillators.push(osc);
            return osc;
        }

        createGain() {
            return node({ gain: { value: 1 } });
        }
    };
}

// --- Bootstrap ---------------------------------------------------------------

// chat.js only constructs Bootstrap components and calls show()/hide(); the
// toasts are what tests care about (in-app notification fallback, errors).
export function makeBootstrap(state) {
    class Component {
        constructor(el, options) {
            this.el = el;
            this.options = options;
        }

        show() {}
        hide() {}
        static getInstance() { return null; }
    }
    class Toast extends Component {
        show() {
            const doc = this.el.ownerDocument;
            state.toasts.push({
                title: doc.getElementById('toast-title')?.textContent,
                body: doc.getElementById('toast-body-content')?.textContent,
                header: doc.getElementById('toast-header')?.className,
            });
        }
    }
    return {
        Toast,
        Modal: class Modal extends Component {},
        Offcanvas: class Offcanvas extends Component {},
        Tooltip: class Tooltip extends Component {},
        Popover: class Popover extends Component {},
        Carousel: class Carousel extends Component {},
    };
}

// CSS.escape per the CSSOM spec (jsdom doesn't ship the CSS namespace).
export function cssEscape(value) {
    const string = String(value);
    let result = '';
    for (let i = 0; i < string.length; i++) {
        const code = string.charCodeAt(i);
        const ch = string.charAt(i);
        if (code === 0) result += '�';
        else if ((code >= 0x1 && code <= 0x1f) || code === 0x7f
            || (i === 0 && code >= 0x30 && code <= 0x39)
            || (i === 1 && code >= 0x30 && code <= 0x39 && string.charCodeAt(0) === 0x2d)) {
            result += `\\${code.toString(16)} `;
        } else if (i === 0 && string.length === 1 && code === 0x2d) result += `\\${ch}`;
        else if (code >= 0x80 || code === 0x2d || code === 0x5f || (code >= 0x30 && code <= 0x39)
            || (code >= 0x41 && code <= 0x5a) || (code >= 0x61 && code <= 0x7a)) result += ch;
        else result += `\\${ch}`;
    }
    return result;
}
