/**
 * D8-Chat on-call pager.
 *
 * Rings on `page` WebSocket frames (dispatched from chat.js) until the page
 * is acknowledged here, from another tab/device (`page_cleared` frame), or
 * the monitor recovers. While ringing: a full-screen alert, the incident's
 * alarm, a flashing tab title and a sticky desktop notification.
 *
 * Each incident carries its policy's alarm: the looping `siren` (outages),
 * the `ka-ching` (website leads from D8), which plays every 6 seconds until
 * the lead is answered, or the `chime` (the interview day), a soft alarm
 * clock every 10 seconds for the first minute and every 20 after that. A new
 * page starts its alarm again (the chime's fast minute too). When only
 * chimes are ringing the alert is a banner in the corner, not the full screen:
 * the person is at work and only needs to know they are wanted. A lead is good
 * news, so its full screen is green, not red.
 *
 * Acknowledge silences this tab at once, before the request. If it fails
 * (e.g. the connection dropped) the page stays up, silenced: the server never
 * heard, so it keeps escalating, and its next re-send rings here again.
 *
 * A lead page the server marks `lead_response` (D8-Chat knows D8's answer
 * endpoint) offers Accept and Reject instead: D8 offered this lead to this
 * person, and D8-Chat's server passes the answer on to D8
 * (POST /oncall/incidents/<id>/accept|reject). Either button silences this
 * tab at once (Reject then asks "are you sure?" in the app's modal). Once the
 * server says the page is settled (answered, or nothing left to answer) it
 * stops here and a toast says how it went, with "Open the lead in D8" after an
 * Accept; D8 also stops the page for everyone, and `page_cleared` may arrive
 * before or after the answer. When the answer can't reach D8 (no connection,
 * a timeout, D8 down) the page rings again with the reason, so the lead is
 * never lost to a failed tap. The incident card's buttons go through htmx
 * instead, and their outcome arrives here as the `lead-response` event.
 *
 * Delivery is belt-and-braces because pub/sub is at-most-once: the server
 * re-sends every page each minute, and this file re-syncs from
 * GET /oncall/pages/active on load, on every WebSocket (re)connect, and every
 * RESYNC_MS while ringing, so a lost `page_cleared` can't leave the siren on.
 *
 * Every alarm plays through Web Audio. The siren's two-tone wail comes from
 * a square-wave LFO modulating the oscillator's frequency on the audio thread,
 * so it keeps going in a background tab where JS timers are throttled. The
 * ka-ching is a recording (static/audio/ka-ching.mp3, the overlay's
 * data-kaching-src), fetched and decoded once the audio context exists; until
 * then, or if it can't load, a synthesized one stands in: a burst of filtered
 * noise (the drawer) and a bell (a few inharmonic partials that ring out). The
 * chime is two soft pairs of sine beeps. For the same reason as the siren,
 * each of those is scheduled on the audio clock minutes ahead, and a timer
 * only tops the schedule up, so a tab whose timers run once a minute still
 * rings on time.
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

    const CUE_LOOKAHEAD_S = 180;        // repeating alarms are scheduled this far ahead on the audio clock
    const CUE_TOPUP_MS = 15000;         // how often their schedule is extended
    // The alarms that repeat a short sound rather than loop: how loud, and when.
    // `gain` is the synthesized sound's, `recordingGain` the recording's (it
    // is already levelled).
    const CUES = {
        // A ka-ching every 6 s until the lead is answered: it never slows down.
        'ka-ching': { gain: 0.5, recordingGain: 1, every: 6, play: 'playKaching' },
        // The chime: every 10 s for the first minute, then every 20 s, and quieter.
        chime: { gain: 0.16, every: 10, slowAfter: 60, slowEvery: 20, play: 'playChime' },
    };
    const KACHING_BELL_HZ = 2093;       // C7, the bell's fundamental
    // [frequency ratio, peak gain, ring-out seconds]: bell-like inharmonic partials.
    const KACHING_PARTIALS = [[1, 0.32, 1.1], [2.0, 0.16, 0.7], [2.76, 0.14, 0.55], [5.4, 0.07, 0.35], [8.93, 0.04, 0.2]];
    const CHIME_HZ = 988;               // B5: an alarm clock's beep, as a sine, not a buzzer
    const CHIME_BEEPS = [0, 0.16, 0.55, 0.71];  // "beep-beep ... beep-beep", seconds from the cue
    const CHIME_BEEP_S = 0.1;

    // How each alarm is worded: the overlay row, the flashing tab title, the
    // desktop notification and its fallback body.
    const ALARM_TEXT = {
        siren: { row: '🔴 DOWN: ', flash: '🚨 PAGE: ', notify: '🚨 DOWN: ', body: 'You are being paged. Open D8-Chat to acknowledge.' },
        'ka-ching': { row: '💰 ', flash: '💰 LEAD: ', notify: '💰 ', body: 'Someone wants to talk to us. Open D8-Chat to accept or reject.' },
        chime: { row: '🔔 ', flash: '🔔 ', notify: '🔔 ', body: "You're needed. Open D8-Chat to acknowledge." },
    };

    // The overlay's heading and footer: an outage (or anything else that
    // isn't good news), a lead (`answer` when it offers Accept and Reject),
    // and the chimes' corner banner.
    const PRESENTATION = {
        alarm: { heading: 'You are being paged', footer: "Acknowledging stops the alarm for everyone and tells the team you're on it." },
        lead: { heading: 'Someone wants to talk to us', footer: "Acknowledging stops the alarm for everyone and tells the team you're on it." },
        answer: { heading: 'Someone wants to talk to us', footer: 'Accept and the lead is yours. Reject and it goes to the next person.' },
        banner: { heading: "You're needed", footer: 'Acknowledging stops the chime and tells the others you have it.' },
    };

    const REJECT_QUESTION = 'Reject this lead? It goes to the next person.';
    // The server gives D8 5 s to answer; this covers that and the trip here.
    const ANSWER_TIMEOUT_MS = 8000;

    const alarmOf = (incident) => (incident && ALARM_TEXT[incident.alarm] ? incident.alarm : 'siren');
    // A website lead from D8 (a test of the ka-ching looks like one too).
    const isLead = ({ incident, test }) => alarmOf(incident) === 'ka-ching' && (test || incident.source === 'd8');
    // A lead this person answers here, with Accept and Reject.
    const isAnswerable = ({ incident, test }) => !test && !!incident.lead_response;

    // Seconds after the alarm started that its n-th cue (from 0) plays.
    function cueOffset(alarm, n) {
        const { every, slowAfter, slowEvery } = CUES[alarm];
        if (!slowAfter) return n * every;
        const fastCount = slowAfter / every;
        if (n <= fastCount) return n * every;
        return slowAfter + (n - fastCount) * slowEvery;
    }

    const kachingOffset = (n) => cueOffset('ka-ching', n);

    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

    function formatTime(iso, withDay) {
        if (!iso) return '';
        const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
        const options = { hour: '2-digit', minute: '2-digit' };
        if (withDay) options.weekday = 'short';
        return date.toLocaleString([], options);
    }

    const PagerManager = {
        // incident id (string) → { incident, test, since, acking, answering, silenced, error }
        entries: new Map(),
        notifications: new Map(),   // incident id → Notification
        clearedAt: new Map(),       // incident id → when it was cleared here (ms)
        audioCtx: null,
        siren: null,
        cues: new Map(),            // alarm → its repeating schedule (ka-ching, chime)
        noise: null,
        kachingLoad: null,          // the recording's fetch + decode, once
        kachingBuffer: null,        // the decoded recording, when it loaded
        titleTimer: null,
        resyncTimer: null,
        originalTitle: document.title,

        init() {
            this.overlay = document.getElementById('pager-overlay');
            this.list = document.getElementById('pager-incidents');
            this.soundHint = document.getElementById('pager-sound-hint');
            this.heading = document.getElementById('pager-heading-text');
            this.footer = document.getElementById('pager-footer');
            if (!this.overlay || !this.list) return;

            // Any click unlocks/resumes audio (browser autoplay policy). Cheap
            // when already running, and it rescues a siren that started on a
            // suspended context before the user had interacted with the page.
            document.addEventListener('click', () => this.ensureAudio(), true);
            document.addEventListener('keydown', () => this.ensureAudio(), true);

            this.list.addEventListener('click', (e) => {
                const ack = e.target.closest('[data-pager-ack]');
                if (ack) this.acknowledge(ack.dataset.pagerAck);
                const answer = e.target.closest('[data-pager-answer]');
                if (answer) this.answerLead(answer.dataset.pagerIncident, answer.dataset.pagerAnswer);
                const view = e.target.closest('[data-pager-view]');
                if (view) this.viewMessage(view.dataset.pagerView, view.dataset.pagerChannel);
            });

            // The incident card's Accept / Reject (htmx): the server's HX-Trigger.
            document.body.addEventListener('lead-response', (e) => this.leadAnswered(e.detail));
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
            // here rings again, unless our acknowledgement (or answer) is
            // still out.
            const acking = !!prev?.acking;
            const answering = !!prev?.answering;
            this.entries.set(id, {
                incident,
                test: !!frame.test,
                since,
                acking,
                answering,
                silenced: acking || answering,
                error: null,
            });
            this.render();
            this.overlay.hidden = false;
            document.body.classList.add('pager-active');
            this.syncAlarms(isNew ? alarmOf(incident) : null);
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
                this.syncAlarms(null);
                return;
            }
            this.overlay.hidden = true;
            document.body.classList.remove('pager-active');
            this.stopSiren();
            this.stopCues();
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
            this.syncAlarms(null);
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

        // --- Accept / Reject (a lead D8 offered to this person) ---

        answerLead(id, action) {
            const entry = this.entries.get(id);
            if (!entry || entry.answering) return;
            // Either button stops the sound here at once, before Reject asks.
            Object.assign(entry, { silenced: true, error: null });
            this.render();
            this.syncAlarms(null);
            if (action !== 'reject') {
                this.sendAnswer(id, action);
                return;
            }
            const send = () => this.sendAnswer(id, action);
            if (window.ConfirmDialog) window.ConfirmDialog.ask(REJECT_QUESTION, send, 'Reject');
            else if (window.confirm(REJECT_QUESTION)) send();
        },

        async sendAnswer(id, action) {
            const entry = this.entries.get(id);
            if (!entry || entry.answering) return;
            Object.assign(entry, { answering: true, silenced: true, error: null });
            this.render();
            this.syncAlarms(null);
            let outcome;
            try {
                outcome = await this.postAnswer(id, action);
            } catch (err) {
                this.answerFailed(id, err.message);
                return;
            }
            // Settled: answered, or nothing left to answer. D8's page_cleared
            // may already have stopped it here; either way, stop.
            this.clear(id);
            this.showLeadOutcome(outcome);
        },

        // The server's outcome, or an Error worded for the person.
        async postAnswer(id, action) {
            const controller = typeof AbortController === 'function' ? new AbortController() : null;
            let timer;
            const timeout = new Promise((resolve, reject) => {
                timer = setTimeout(() => {
                    reject(new Error("D8-Chat didn't answer in time. Try again."));
                    controller?.abort();
                }, ANSWER_TIMEOUT_MS);
            });
            const request = fetch(`/oncall/incidents/${encodeURIComponent(id)}/${action}`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': csrfToken(), Accept: 'application/json' },
                signal: controller?.signal,
            }).catch((err) => {
                throw new Error(`Couldn't reach D8-Chat (${err.message}). Try again.`);
            });
            try {
                const response = await Promise.race([request, timeout]);
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.error || `D8-Chat had a problem (HTTP ${response.status}). Try again.`);
                }
                return data;
            } finally {
                clearTimeout(timer);
            }
        },

        // D8 never took the answer: ring again, so the lead isn't lost.
        answerFailed(id, message) {
            const current = this.entries.get(id);
            if (!current) {
                // The page stopped meanwhile (D8 resolved it).
                window.ToastManager?.show("Couldn't send your answer", message, 'danger');
                return;
            }
            Object.assign(current, { answering: false, silenced: false, error: message });
            this.render();
            this.syncAlarms(null);
        },

        // An answer settled from the incident card (htmx's lead-response event).
        leadAnswered(outcome) {
            if (!outcome) return;
            if (outcome.incident_id != null) this.clear(String(outcome.incident_id));
            this.showLeadOutcome(outcome);
        },

        showLeadOutcome(outcome) {
            const toast = window.ToastManager;
            if (!toast) return;
            if (outcome.ok && outcome.status === 'accepted') {
                const link = outcome.lead_url ? { href: outcome.lead_url, text: 'Open the lead in D8' } : null;
                // Stays up until closed, so the link is there when they want it.
                toast.show('Lead accepted', outcome.message, 'success', false, link);
            } else if (outcome.ok) {
                toast.show('Lead rejected', outcome.message, 'secondary');
            } else {
                toast.show('Nothing to answer', outcome.message, 'secondary');
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

        // Only chimes ringing: a banner in the corner instead of the full
        // screen. Leads (with or without chimes): the green full screen.
        // Anything else: the red one.
        presentation() {
            const entries = [...this.entries.values()];
            const chime = (entry) => alarmOf(entry.incident) === 'chime';
            if (entries.every(chime)) return 'banner';
            if (entries.every((entry) => isLead(entry) || chime(entry))) return 'lead';
            return 'alarm';
        },

        setPresentation(mode) {
            this.overlay.classList.toggle('pager-overlay--banner', mode === 'banner');
            this.overlay.classList.toggle('pager-overlay--lead', mode === 'lead');
            this.overlay.setAttribute('aria-modal', mode === 'banner' ? 'false' : 'true');
            const answer = mode === 'lead' && [...this.entries.values()].some(isAnswerable);
            const text = PRESENTATION[answer ? 'answer' : mode];
            if (this.heading) this.heading.textContent = text.heading;
            if (this.footer) this.footer.textContent = text.footer;
        },

        render() {
            const mode = this.presentation();
            const banner = mode === 'banner';
            this.setPresentation(mode);
            this.list.replaceChildren();
            for (const [id, entry] of this.entries) {
                const { incident, test, acking, answering, silenced, error } = entry;
                const answerable = isAnswerable(entry);
                const row = document.createElement('div');
                row.className = 'pager-incident';
                row.dataset.incidentId = id;

                const title = document.createElement('div');
                title.className = 'pager-incident-title';
                title.textContent = test
                    ? `🧪 ${incident.title}`
                    : `${ALARM_TEXT[alarmOf(incident)].row}${incident.title}`;
                row.appendChild(title);

                // A page request (a D8 lead, the interview day) links what it is
                // about instead of showing the URL.
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
                if (answerable) {
                    actions.appendChild(this.answerButton(id, 'accept', 'Accept', 'btn btn-light btn-lg fw-bold', answering));
                    actions.appendChild(this.answerButton(id, 'reject', 'Reject', 'btn btn-outline-light btn-lg', answering));
                } else {
                    const ack = document.createElement('button');
                    ack.type = 'button';
                    ack.className = banner ? 'btn btn-primary fw-bold' : 'btn btn-light btn-lg fw-bold';
                    ack.dataset.pagerAck = id;
                    ack.disabled = !!acking;
                    ack.textContent = test ? 'Stop test' : 'Acknowledge';
                    actions.appendChild(ack);
                }
                // Answered here, the lead's link comes with the outcome.
                if (leadLink && !answerable) {
                    const open = document.createElement('a');
                    open.className = banner ? 'btn btn-outline-primary' : 'btn btn-outline-light';
                    open.href = leadLink;
                    open.target = '_blank';
                    open.rel = 'noopener noreferrer';
                    open.textContent = alarmOf(incident) === 'ka-ching' ? 'Open the lead in D8' : 'Open';
                    actions.appendChild(open);
                }
                if (incident.message_id) {
                    const view = document.createElement('button');
                    view.type = 'button';
                    view.className = banner ? 'btn btn-outline-secondary' : 'btn btn-outline-light';
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
                if (silenced && !acking && !answering) {
                    const note = document.createElement('div');
                    note.className = 'pager-incident-silenced';
                    note.textContent = answerable
                        ? '🔇 Silenced in this tab. It rings again until you accept or reject.'
                        : '🔇 Silenced in this tab. The page keeps escalating until someone acknowledges.';
                    row.appendChild(note);
                }

                this.list.appendChild(row);
            }
        },

        answerButton(id, action, label, className, disabled) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = className;
            button.dataset.pagerAnswer = action;
            button.dataset.pagerIncident = id;
            button.disabled = !!disabled;
            button.textContent = label;
            return button;
        },

        // --- Sound ---

        ensureAudio() {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            if (!this.audioCtx) {
                this.audioCtx = new Ctx();
                this.audioCtx.addEventListener('statechange', () => this.updateSoundHint());
                this.loadKaching(this.audioCtx);
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
        // `restart`: the alarm of a page that just arrived, whose fast first
        // minute starts again.
        syncAlarms(restart) {
            // A page silenced here (Acknowledge tapped, the request still out or
            // failed) makes no sound until its next re-send.
            const alarms = new Set(
                [...this.entries.values()]
                    .filter((entry) => !entry.silenced)
                    .map(({ incident }) => alarmOf(incident)),
            );
            if (alarms.has('siren')) this.startSiren();
            else this.stopSiren();
            for (const alarm of Object.keys(CUES)) {
                if (alarms.has(alarm)) this.startCue(alarm, restart === alarm);
                else this.stopCue(alarm);
            }
        },

        // `origin`: when the schedule's first cue was due (the audio clock),
        // to keep a running alarm's rhythm; a new one starts now.
        startCue(alarm, restart, origin = null) {
            const ctx = this.ensureAudio();
            if (!ctx || (this.cues.has(alarm) && !restart)) {
                this.updateSoundHint();
                return;
            }
            this.stopCue(alarm);
            const master = ctx.createGain();
            master.gain.value = this.usesRecording(alarm) ? CUES[alarm].recordingGain : CUES[alarm].gain;
            master.connect(ctx.destination);
            this.cues.set(alarm, {
                master,
                origin: origin ?? ctx.currentTime + 0.05,
                next: 0,
                sources: new Set(),
                timer: setInterval(() => this.scheduleCue(alarm), CUE_TOPUP_MS),
            });
            this.scheduleCue(alarm);
            this.updateSoundHint();
        },

        // Schedule every cue due before the lookahead horizon. One whose time
        // has already passed (the tab slept past it) is skipped, so the alarm
        // never plays a backlog all at once.
        scheduleCue(alarm) {
            const cue = this.cues.get(alarm);
            if (!cue) return;
            const now = this.audioCtx.currentTime;
            const horizon = now + CUE_LOOKAHEAD_S;
            while (cue.origin + cueOffset(alarm, cue.next) <= horizon) {
                const at = cue.origin + cueOffset(alarm, cue.next);
                if (at >= now) this[CUES[alarm].play](at, cue);
                cue.next += 1;
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

        // Start `source` at `start`, stop it at `stop`, and keep it in the
        // cue's set until it ends, so stopping the alarm can free it.
        trackSource(cue, source, start, stop) {
            cue.sources.add(source);
            source.onended = () => cue.sources.delete(source);
            source.start(start);
            source.stop(stop);
        },

        usesRecording(alarm) {
            return alarm === 'ka-ching' && !!this.kachingBuffer;
        },

        // Fetch and decode the recorded ka-ching once. Ka-chings already
        // scheduled were synthesized, so a ringing one is rescheduled with the
        // recording, on the same beat. If it can't load or decode, the
        // synthesized one stays.
        loadKaching(ctx) {
            const src = this.overlay?.dataset.kachingSrc;
            if (this.kachingLoad || !src || typeof ctx.decodeAudioData !== 'function') return;
            this.kachingLoad = fetch(src, { credentials: 'same-origin' })
                .then((response) => {
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    return response.arrayBuffer();
                })
                .then((bytes) => ctx.decodeAudioData(bytes))
                .then((buffer) => {
                    this.kachingBuffer = buffer;
                    const cue = this.cues.get('ka-ching');
                    if (cue) this.startCue('ka-ching', true, cue.origin);
                })
                .catch((err) => console.warn('Pager: playing the synthesized ka-ching; the recording failed to load:', err));
        },

        playKaching(at, cue) {
            if (this.kachingBuffer) {
                this.playRecording(at, cue);
                return;
            }
            const ctx = this.audioCtx;

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
            noise.connect(band).connect(clack).connect(cue.master);
            this.trackSource(cue, noise, at, at + 0.08);

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
                osc.connect(envelope).connect(cue.master);
                this.trackSource(cue, osc, strike, strike + ringOut + 0.05);
            }
        },

        playRecording(at, cue) {
            const source = this.audioCtx.createBufferSource();
            source.buffer = this.kachingBuffer;
            source.connect(cue.master);
            this.trackSource(cue, source, at, at + this.kachingBuffer.duration + 0.05);
        },

        // Two pairs of short sine beeps, eased in and out so none of them clicks.
        playChime(at, cue) {
            const ctx = this.audioCtx;
            for (const offset of CHIME_BEEPS) {
                const start = at + offset;
                const osc = ctx.createOscillator();
                osc.type = 'sine';
                osc.frequency.value = CHIME_HZ;
                const envelope = ctx.createGain();
                envelope.gain.setValueAtTime(0.0001, start);
                envelope.gain.exponentialRampToValueAtTime(1, start + 0.015);
                envelope.gain.setValueAtTime(1, start + CHIME_BEEP_S - 0.03);
                envelope.gain.exponentialRampToValueAtTime(0.0001, start + CHIME_BEEP_S);
                osc.connect(envelope).connect(cue.master);
                this.trackSource(cue, osc, start, start + CHIME_BEEP_S + 0.02);
            }
        },

        stopCue(alarm) {
            const cue = this.cues.get(alarm);
            if (!cue) return;
            clearInterval(cue.timer);
            // Disconnecting the master silences everything already scheduled;
            // stopping the sources frees them.
            cue.master.disconnect();
            for (const source of cue.sources) {
                try {
                    source.stop();
                } catch (err) { /* already stopped */ }
            }
            this.cues.delete(alarm);
            this.updateSoundHint();
        },

        stopCues() {
            for (const alarm of [...this.cues.keys()]) this.stopCue(alarm);
        },

        updateSoundHint() {
            if (!this.soundHint) return;
            const sounding = !!this.siren || this.cues.size > 0;
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
    PagerManager.cueOffset = cueOffset;
    window.PagerManager = PagerManager;
    document.addEventListener('DOMContentLoaded', () => PagerManager.init());
})();
