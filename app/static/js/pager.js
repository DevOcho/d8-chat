/**
 * D8-Chat on-call pager.
 *
 * Rings on `page` WebSocket frames (dispatched from chat.js) until the page
 * is acknowledged here, from another tab/device (`page_cleared` frame), or
 * the monitor recovers. While ringing: a full-screen alert, a looping siren,
 * a flashing tab title and a sticky desktop notification.
 *
 * Delivery is belt-and-braces because pub/sub is at-most-once: the server
 * re-sends every page each minute, and this file re-syncs from
 * GET /oncall/pages/active on load, on every WebSocket (re)connect, and every
 * RESYNC_MS while ringing, so a lost `page_cleared` can't leave the siren on.
 *
 * The siren is synthesized with Web Audio (no asset). Its two-tone wail comes
 * from a square-wave LFO modulating the oscillator's frequency on the audio
 * thread, so it keeps going in a background tab where JS timers are throttled.
 * Browsers only allow audio after a user gesture on the page; until then the
 * alert shows a "click to start the alarm" hint.
 */
(function () {
    'use strict';

    const SIREN_GAIN = 0.22;        // square waves are harsh; this is plenty loud
    const SIREN_CENTER_HZ = 865;    // swings ±175 Hz → ~690 / ~1040 Hz
    const SIREN_SWING_HZ = 175;
    const SIREN_RATE_HZ = 1.6;      // tone changes per second / 2
    const RESYNC_MS = 30000;        // while ringing, re-check what's still active

    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

    function formatTime(iso, withDay) {
        if (!iso) return '';
        const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
        const options = { hour: '2-digit', minute: '2-digit' };
        if (withDay) options.weekday = 'short';
        return date.toLocaleString([], options);
    }

    const PagerManager = {
        entries: new Map(),         // incident id (string) → { incident, test, since }
        notifications: new Map(),   // incident id → Notification
        clearedAt: new Map(),       // incident id → when it was cleared here (ms)
        audioCtx: null,
        siren: null,
        titleTimer: null,
        resyncTimer: null,
        originalTitle: document.title,

        init() {
            this.overlay = document.getElementById('pager-overlay');
            this.list = document.getElementById('pager-incidents');
            this.soundHint = document.getElementById('pager-sound-hint');
            if (!this.overlay || !this.list) return;

            // Any click unlocks/resumes audio (browser autoplay policy). Cheap
            // when already running, and it rescues a siren that started on a
            // suspended context before the user had interacted with the page.
            document.addEventListener('click', () => this.ensureAudio(), true);
            document.addEventListener('keydown', () => this.ensureAudio(), true);

            this.list.addEventListener('click', (e) => {
                const ack = e.target.closest('[data-pager-ack]');
                if (ack) this.acknowledge(ack.dataset.pagerAck, ack);
                const view = e.target.closest('[data-pager-view]');
                if (view) this.viewMessage(view.dataset.pagerView, view.dataset.pagerChannel);
            });

            document.body.addEventListener('htmx:wsOpen', () => this.syncActive());
            // Background tabs throttle timers, so also check on coming back.
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible' && this.entries.size) {
                    this.syncActive();
                }
            });
            ['htmx:afterSwap', 'htmx:oobAfterSwap'].forEach((name) => {
                document.body.addEventListener(name, () => {
                    setTimeout(() => this.formatTimes(document), 0);
                });
            });
            this.formatTimes(document);
            this.syncActive();
        },

        handleFrame(data) {
            if (data.type === 'page' && data.incident) this.page(data.incident, data);
            else if (data.type === 'page_cleared') this.clear(String(data.incident_id));
        },

        page(incident, frame = {}) {
            const id = String(incident.id);
            const isNew = !this.entries.has(id);
            const since = isNew ? Date.now() : this.entries.get(id).since;
            this.entries.set(id, { incident, test: !!frame.test, since });
            this.render();
            this.overlay.hidden = false;
            document.body.classList.add('pager-active');
            this.startSiren();
            this.startTitleFlash();
            this.startResync();
            this.notify(incident, !isNew);
        },

        clear(id) {
            if (!this.entries.delete(id)) return;
            this.clearedAt.set(id, Date.now());
            const notification = this.notifications.get(id);
            if (notification) notification.close();
            this.notifications.delete(id);
            if (this.entries.size) {
                this.render();
                return;
            }
            this.overlay.hidden = true;
            document.body.classList.remove('pager-active');
            this.stopSiren();
            this.stopTitleFlash();
            this.stopResync();
        },

        // A `page_cleared` frame lost while the socket was reconnecting would
        // leave the siren on for an incident someone already acknowledged, so
        // poll the server's view while anything is ringing.
        startResync() {
            if (this.resyncTimer) return;
            this.resyncTimer = setInterval(() => this.syncActive(), RESYNC_MS);
        },

        stopResync() {
            clearInterval(this.resyncTimer);
            this.resyncTimer = null;
        },

        async acknowledge(id, button) {
            const entry = this.entries.get(id);
            if (!entry) return;
            if (entry.test) {
                this.clear(id);
                return;
            }
            button.disabled = true;
            try {
                const response = await fetch(`/oncall/incidents/${encodeURIComponent(id)}/ack`, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-CSRFToken': csrfToken(), Accept: 'application/json' },
                });
                if (!response.ok) {
                    const data = await response.json().catch(() => ({}));
                    throw new Error(data.error || `HTTP ${response.status}`);
                }
                // Acknowledged (or someone beat us to it) — either way, stop.
                this.clear(id);
            } catch (err) {
                button.disabled = false;
                this.showError(id, `Couldn't acknowledge: ${err.message}. Try again.`);
            }
        },

        viewMessage(messageId, channelId) {
            this.ensureAudio();
            const selector = `#message-${messageId}`;
            if (document.querySelector(selector)) {
                document.querySelector(selector).scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else if (window.htmx) {
                window.htmx.ajax('GET', `/chat/message/${encodeURIComponent(messageId)}/context`, {
                    target: '#chat-messages-container',
                    swap: 'innerHTML',
                });
            } else if (channelId) {
                window.location.href = '/chat';
            }
        },

        async syncActive() {
            const startedAt = Date.now();
            try {
                const response = await fetch('/oncall/pages/active', {
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                });
                if (!response.ok) return;
                const data = await response.json();
                const live = new Set(data.incidents.map((i) => String(i.id)));
                for (const [id, entry] of this.entries) {
                    // A page that arrived after this request left isn't in its
                    // (older) answer yet; clearing it would silence a new page.
                    if (!entry.test && !live.has(id) && entry.since < startedAt) this.clear(id);
                }
                data.incidents.forEach((incident) => {
                    const id = String(incident.id);
                    // Already ringing: leave it (no re-notify every poll).
                    // Cleared while this request was in flight: the answer is
                    // stale, so don't bring it back.
                    if (this.entries.has(id) || this.clearedAt.get(id) >= startedAt) return;
                    this.page(incident, { repeat: true });
                });
                for (const [id, at] of this.clearedAt) {
                    if (startedAt - at > RESYNC_MS * 4) this.clearedAt.delete(id);
                }
            } catch (err) {
                console.warn('Pager sync failed:', err);
            }
        },

        render() {
            this.list.replaceChildren();
            for (const [id, { incident, test }] of this.entries) {
                const row = document.createElement('div');
                row.className = 'pager-incident';
                row.dataset.incidentId = id;

                const title = document.createElement('div');
                title.className = 'pager-incident-title';
                title.textContent = test ? `🧪 ${incident.title}` : `🔴 DOWN: ${incident.title}`;
                row.appendChild(title);

                const meta = [];
                if (incident.source_url) meta.push(incident.source_url);
                if (incident.channel_name) meta.push(`#${incident.channel_name}`);
                if (incident.triggered_at) meta.push(`since ${formatTime(incident.triggered_at)}`);
                if (meta.length) {
                    const metaEl = document.createElement('div');
                    metaEl.className = 'pager-incident-meta';
                    metaEl.textContent = meta.join(' · ');
                    row.appendChild(metaEl);
                }
                if (incident.details) {
                    const details = document.createElement('div');
                    details.className = 'pager-incident-details';
                    details.textContent = incident.details;
                    row.appendChild(details);
                }
                const paged = (incident.paged_users || [])
                    .map((u) => u.display_name || u.username).join(', ');
                if (paged && !test) {
                    const pagedEl = document.createElement('div');
                    pagedEl.className = 'pager-incident-meta';
                    pagedEl.textContent = `Paging: ${paged}` +
                        (incident.next_escalation_at
                            ? ` · escalates at ${formatTime(incident.next_escalation_at)}`
                            : '');
                    row.appendChild(pagedEl);
                }

                const actions = document.createElement('div');
                actions.className = 'pager-incident-actions';
                const ack = document.createElement('button');
                ack.type = 'button';
                ack.className = 'btn btn-light btn-lg fw-bold';
                ack.dataset.pagerAck = id;
                ack.textContent = test ? 'Stop test' : 'Acknowledge';
                actions.appendChild(ack);
                if (incident.message_id) {
                    const view = document.createElement('button');
                    view.type = 'button';
                    view.className = 'btn btn-outline-light';
                    view.dataset.pagerView = incident.message_id;
                    view.dataset.pagerChannel = incident.channel_id || '';
                    view.textContent = 'View in channel';
                    actions.appendChild(view);
                }
                row.appendChild(actions);

                const error = document.createElement('div');
                error.className = 'pager-incident-error';
                error.hidden = true;
                row.appendChild(error);

                this.list.appendChild(row);
            }
        },

        showError(id, message) {
            const row = this.list.querySelector(`[data-incident-id="${CSS.escape(id)}"] .pager-incident-error`);
            if (row) {
                row.textContent = message;
                row.hidden = false;
            }
        },

        // --- Sound ---

        ensureAudio() {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            if (!this.audioCtx) {
                this.audioCtx = new Ctx();
                this.audioCtx.addEventListener('statechange', () => this.updateSoundHint());
            }
            if (this.audioCtx.state === 'suspended') {
                this.audioCtx.resume().catch(() => {}).finally(() => this.updateSoundHint());
            }
            return this.audioCtx;
        },

        startSiren() {
            const ctx = this.ensureAudio();
            if (!ctx || this.siren) {
                this.updateSoundHint();
                return;
            }
            const osc = ctx.createOscillator();
            osc.type = 'square';
            osc.frequency.value = SIREN_CENTER_HZ;

            const lfo = ctx.createOscillator();
            lfo.type = 'square';
            lfo.frequency.value = SIREN_RATE_HZ;
            const lfoDepth = ctx.createGain();
            lfoDepth.gain.value = SIREN_SWING_HZ;
            lfo.connect(lfoDepth).connect(osc.frequency);

            const volume = ctx.createGain();
            volume.gain.value = SIREN_GAIN;
            osc.connect(volume).connect(ctx.destination);

            osc.start();
            lfo.start();
            this.siren = { osc, lfo, lfoDepth, volume };
            this.updateSoundHint();
        },

        stopSiren() {
            if (!this.siren) return;
            const { osc, lfo, lfoDepth, volume } = this.siren;
            try {
                osc.stop();
                lfo.stop();
            } catch (err) { /* already stopped */ }
            [osc, lfo, lfoDepth, volume].forEach((node) => node.disconnect());
            this.siren = null;
            this.updateSoundHint();
        },

        updateSoundHint() {
            if (!this.soundHint) return;
            const blocked = !!this.siren && (!this.audioCtx || this.audioCtx.state !== 'running');
            this.soundHint.hidden = !blocked;
        },

        // --- Title + desktop notification ---

        startTitleFlash() {
            if (this.titleTimer) return;
            let on = false;
            this.titleTimer = setInterval(() => {
                on = !on;
                const first = this.entries.values().next().value;
                document.title = on && first ? `🚨 PAGE: ${first.incident.title}` : this.baseTitle();
            }, 1000);
        },

        stopTitleFlash() {
            clearInterval(this.titleTimer);
            this.titleTimer = null;
            document.title = this.baseTitle();
        },

        // The title chat.js would show right now (the open conversation and
        // unread count), not the one captured when the page loaded.
        baseTitle() {
            return window.TitleManager ? window.TitleManager.current() : this.originalTitle;
        },

        notify(incident, renotify) {
            if (!('Notification' in window) || Notification.permission !== 'granted') return;
            const id = String(incident.id);
            try {
                const notification = new Notification(`🚨 DOWN: ${incident.title}`, {
                    body: incident.details || 'You are being paged. Open D8-Chat to acknowledge.',
                    tag: `incident-${id}`,
                    requireInteraction: true,
                    renotify,
                    icon: document.querySelector('link[rel="icon"][sizes="32x32"]')?.href
                        || '/favicon-32x32.png',
                });
                notification.onclick = () => {
                    window.focus();
                    this.ensureAudio();
                };
                this.notifications.set(id, notification);
            } catch (err) {
                console.warn('Pager notification failed:', err);
            }
        },

        // --- Incident card / badge timestamps ---

        formatTimes(root) {
            root.querySelectorAll('.incident-time:not(.time-processed)').forEach((el) => {
                if (el.dataset.timestamp) {
                    el.textContent = formatTime(el.dataset.timestamp, el.dataset.format === 'datetime');
                }
                el.classList.add('time-processed');
            });
        },
    };

    window.PagerManager = PagerManager;
    document.addEventListener('DOMContentLoaded', () => PagerManager.init());
})();
