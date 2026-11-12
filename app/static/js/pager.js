/**
 * D8-Chat on-call pager.
 *
 * Rings on `page` WebSocket frames (dispatched from chat.js) until the page
 * is acknowledged here, from another tab/device (`page_cleared` frame), or
 * the monitor recovers. While ringing: a full-screen alert, the incident's
 * alarm, a flashing tab title and a sticky desktop notification.
 *
 * Each incident carries its policy's alarm: the looping `siren` (outages) or
 * the `ka-ching` (website leads from D8), which plays every 5 seconds for the
 * first minute and every 10 seconds after that. A new lead starts the fast
 * minute again.
 *
 * Acknowledge silences this tab at once, before the request. If it fails
 * (e.g. the connection dropped) the page stays up, silenced: the server never
 * heard, so it keeps escalating, and its next re-send rings here again.
 *
 * Delivery is belt-and-braces because pub/sub is at-most-once: the server
 * re-sends every page each minute, and this file re-syncs from
 * GET /oncall/pages/active on load, on every WebSocket (re)connect, and every
 * RESYNC_MS while ringing, so a lost `page_cleared` can't leave the siren on.
 *
 * Both alarms are synthesized with Web Audio (no asset). The siren's two-tone
 * wail comes from a square-wave LFO modulating the oscillator's frequency on
 * the audio thread, so it keeps going in a background tab where JS timers are
 * throttled. The ka-ching is a burst of filtered noise (the drawer) and a bell
 * (a few inharmonic partials that ring out); for the same reason each one is
 * scheduled on the audio clock minutes ahead, and a timer only tops the
 * schedule up, so a tab whose timers run once a minute still rings on time.
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

    const KACHING_GAIN = 0.5;
    const KACHING_FAST_EVERY_S = 5;     // a ka-ching every 5 s ...
    const KACHING_FAST_FOR_S = 60;      // ... for the first minute,
    const KACHING_SLOW_EVERY_S = 10;    // then every 10 s
    const KACHING_LOOKAHEAD_S = 180;    // scheduled this far ahead on the audio clock
    const KACHING_TOPUP_MS = 15000;     // how often the schedule is extended
    const KACHING_BELL_HZ = 2093;       // C7, the bell's fundamental
    // [frequency ratio, peak gain, ring-out seconds]: bell-like inharmonic partials.
    const KACHING_PARTIALS = [[1, 0.32, 1.1], [2.0, 0.16, 0.7], [2.76, 0.14, 0.55], [5.4, 0.07, 0.35], [8.93, 0.04, 0.2]];

    // How each alarm is worded: the overlay row, the flashing tab title, the
    // desktop notification and its fallback body.
    const ALARM_TEXT = {
        siren: { row: '🔴 DOWN: ', flash: '🚨 PAGE: ', notify: '🚨 DOWN: ', body: 'You are being paged. Open D8-Chat to acknowledge.' },
        'ka-ching': { row: '💰 ', flash: '💰 LEAD: ', notify: '💰 ', body: 'Someone wants to talk to us. Open D8-Chat to acknowledge.' },
    };

    const alarmOf = (incident) => (incident && incident.alarm === 'ka-ching' ? 'ka-ching' : 'siren');

    // Seconds after the alarm started that the n-th ka-ching (from 0) plays.
    function kachingOffset(n) {
        const fastCount = KACHING_FAST_FOR_S / KACHING_FAST_EVERY_S;
        if (n <= fastCount) return n * KACHING_FAST_EVERY_S;
        return KACHING_FAST_FOR_S + (n - fastCount) * KACHING_SLOW_EVERY_S;
    }

    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

    function formatTime(iso, withDay) {
        if (!iso) return '';
        const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
        const options = { hour: '2-digit', minute: '2-digit' };
        if (withDay) options.weekday = 'short';
        return date.toLocaleString([], options);
    }

    const PagerManager = {
        // incident id (string) → { incident, test, since, acking, silenced, error }
        entries: new Map(),
        notifications: new Map(),   // incident id → Notification
        clearedAt: new Map(),       // incident id → when it was cleared here (ms)
        audioCtx: null,
        siren: null,
        kaching: null,
        noise: null,
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
                if (ack) this.acknowledge(ack.dataset.pagerAck);
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
            const prev = this.entries.get(id);
            const isNew = !prev;
            const since = isNew ? Date.now() : prev.since;
            // A re-send means nobody has acknowledged it, so a page silenced
            // here rings again, unless our acknowledgement is still out.
            const acking = !!prev?.acking;
            this.entries.set(id, {
                incident,
                test: !!frame.test,
                since,
                acking,
                silenced: acking,
                error: null,
            });
            this.render();
            this.overlay.hidden = false;
            document.body.classList.add('pager-active');
            this.syncAlarms(isNew && alarmOf(incident) === 'ka-ching');
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
                this.syncAlarms(false);
                return;
            }
            this.overlay.hidden = true;
            document.body.classList.remove('pager-active');
            this.stopSiren();
            this.stopKaching();
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

        async acknowledge(id) {
            const entry = this.entries.get(id);
            if (!entry || entry.acking) return;
            if (entry.test) {
                this.clear(id);
                return;
            }
            // Silence first: with no connection the request can take a while
            // to fail.
            Object.assign(entry, { acking: true, silenced: true, error: null });
            this.render();
            this.syncAlarms(false);
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
                // page() may have replaced the entry while the request was out.
                const current = this.entries.get(id);
                if (!current) return;
                current.acking = false;
                current.error = `Couldn't acknowledge: ${err.message}. Try again.`;
                this.render();
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
            for (const [id, { incident, test, acking, silenced, error }] of this.entries) {
                const row = document.createElement('div');
                row.className = 'pager-incident';
                row.dataset.incidentId = id;

                const title = document.createElement('div');
                title.className = 'pager-incident-title';
                title.textContent = test
                    ? `🧪 ${incident.title}`
                    : `${ALARM_TEXT[alarmOf(incident)].row}${incident.title}`;
                row.appendChild(title);

                // A D8 page links the lead in D8 instead of showing the URL.
                const leadLink = incident.source === 'd8' && /^https?:\/\//.test(incident.source_url || '')
                    ? incident.source_url : null;
                const meta = [];
                if (incident.source_url && !leadLink) meta.push(incident.source_url);
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
                ack.disabled = !!acking;
                ack.textContent = test ? 'Stop test' : 'Acknowledge';
                actions.appendChild(ack);
                if (leadLink) {
                    const open = document.createElement('a');
                    open.className = 'btn btn-outline-light';
                    open.href = leadLink;
                    open.target = '_blank';
                    open.rel = 'noopener noreferrer';
                    open.textContent = 'Open the lead in D8';
                    actions.appendChild(open);
                }
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

                const errorEl = document.createElement('div');
                errorEl.className = 'pager-incident-error';
                errorEl.textContent = error || '';
                errorEl.hidden = !error;
                row.appendChild(errorEl);

                // Not while the request is out: it may still go through.
                if (silenced && !acking) {
                    const note = document.createElement('div');
                    note.className = 'pager-incident-silenced';
                    note.textContent = '🔇 Silenced in this tab. The page keeps escalating until someone acknowledges.';
                    row.appendChild(note);
                }

                this.list.appendChild(row);
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

        // Play every alarm a ringing incident asks for, and stop the others.
        // `restartKaching`: a new lead starts the fast first minute again.
        syncAlarms(restartKaching) {
            // A page silenced here (Acknowledge tapped, the request still out or
            // failed) makes no sound until its next re-send.
            const alarms = new Set(
                [...this.entries.values()]
                    .filter((entry) => !entry.silenced)
                    .map(({ incident }) => alarmOf(incident)),
            );
            if (alarms.has('siren')) this.startSiren();
            else this.stopSiren();
            if (alarms.has('ka-ching')) this.startKaching(restartKaching);
            else this.stopKaching();
        },

        startKaching(restart) {
            const ctx = this.ensureAudio();
            if (!ctx || (this.kaching && !restart)) {
                this.updateSoundHint();
                return;
            }
            this.stopKaching();
            const master = ctx.createGain();
            master.gain.value = KACHING_GAIN;
            master.connect(ctx.destination);
            this.kaching = {
                master,
                origin: ctx.currentTime + 0.05,
                next: 0,
                sources: new Set(),
                timer: setInterval(() => this.scheduleKaching(), KACHING_TOPUP_MS),
            };
            this.scheduleKaching();
            this.updateSoundHint();
        },

        // Schedule every ka-ching due before the lookahead horizon. One whose
        // time has already passed (the tab slept past it) is skipped, so the
        // alarm never plays a backlog all at once.
        scheduleKaching() {
            const kaching = this.kaching;
            if (!kaching) return;
            const now = this.audioCtx.currentTime;
            const horizon = now + KACHING_LOOKAHEAD_S;
            while (kaching.origin + kachingOffset(kaching.next) <= horizon) {
                const at = kaching.origin + kachingOffset(kaching.next);
                if (at >= now) this.playKaching(at);
                kaching.next += 1;
            }
        },

        noiseBuffer(ctx) {
            if (!this.noise) {
                const length = Math.round(ctx.sampleRate * 0.08);
                this.noise = ctx.createBuffer(1, length, ctx.sampleRate);
                const data = this.noise.getChannelData(0);
                // Audio noise, not a secret.
                for (let i = 0; i < length; i += 1) data[i] = Math.random() * 2 - 1;
            }
            return this.noise;
        },

        playKaching(at) {
            const ctx = this.audioCtx;
            const { master, sources } = this.kaching;
            const track = (source, start, stop) => {
                sources.add(source);
                source.onended = () => sources.delete(source);
                source.start(start);
                source.stop(stop);
            };

            // "ka": a short burst of band-passed noise.
            const noise = ctx.createBufferSource();
            noise.buffer = this.noiseBuffer(ctx);
            const band = ctx.createBiquadFilter();
            band.type = 'bandpass';
            band.frequency.value = 2400;
            band.Q.value = 0.8;
            const clack = ctx.createGain();
            clack.gain.setValueAtTime(0.0001, at);
            clack.gain.exponentialRampToValueAtTime(0.6, at + 0.004);
            clack.gain.exponentialRampToValueAtTime(0.0001, at + 0.06);
            noise.connect(band).connect(clack).connect(master);
            track(noise, at, at + 0.08);

            // "ching": the bell, struck just after.
            const strike = at + 0.09;
            for (const [ratio, peak, ringOut] of KACHING_PARTIALS) {
                const osc = ctx.createOscillator();
                osc.type = 'sine';
                osc.frequency.value = KACHING_BELL_HZ * ratio;
                const envelope = ctx.createGain();
                envelope.gain.setValueAtTime(0.0001, strike);
                envelope.gain.exponentialRampToValueAtTime(peak, strike + 0.003);
                envelope.gain.exponentialRampToValueAtTime(0.0001, strike + ringOut);
                osc.connect(envelope).connect(master);
                track(osc, strike, strike + ringOut + 0.05);
            }
        },

        stopKaching() {
            if (!this.kaching) return;
            const { master, sources, timer } = this.kaching;
            clearInterval(timer);
            // Disconnecting the master silences everything already scheduled;
            // stopping the sources frees them.
            master.disconnect();
            for (const source of sources) {
                try {
                    source.stop();
                } catch (err) { /* already stopped */ }
            }
            this.kaching = null;
            this.updateSoundHint();
        },

        updateSoundHint() {
            if (!this.soundHint) return;
            const sounding = !!this.siren || !!this.kaching;
            const blocked = sounding && (!this.audioCtx || this.audioCtx.state !== 'running');
            this.soundHint.hidden = !blocked;
        },

        // --- Title + desktop notification ---

        startTitleFlash() {
            if (this.titleTimer) return;
            let on = false;
            this.titleTimer = setInterval(() => {
                on = !on;
                const first = this.entries.values().next().value;
                document.title = on && first
                    ? `${ALARM_TEXT[alarmOf(first.incident)].flash}${first.incident.title}`
                    : this.baseTitle();
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
            const text = ALARM_TEXT[alarmOf(incident)];
            try {
                const notification = new Notification(`${text.notify}${incident.title}`, {
                    body: incident.details || text.body,
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

    PagerManager.kachingOffset = kachingOffset;
    window.PagerManager = PagerManager;
    document.addEventListener('DOMContentLoaded', () => PagerManager.init());
})();
