// Realtime HTML frames handled by htmx-ws + chat.js: message appends with
// their date dividers, timestamp localization, dedupe against the reconnect
// backfill, edits, reactions, thread replies and the scroll / "Jump to Latest"
// behaviour.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    ENGINEER, ME, chatPage, conversationPane, dateSeparator, message, messageFrame, oobToSelector,
} from './helpers/fixtures.js';

// The server renders "%I:%M %p" in UTC as a fallback; the browser rewrites it
// in the viewer's zone. The suite runs in America/New_York (vitest.config.js),
// which is UTC-4 on these dates.
const UTC_AFTERNOON = '2026-10-17T18:42:07.123456'; // isoformat() with microseconds
const LOCAL_AFTERNOON = '02:42 PM';

const yesterdayRows = [
    dateSeparator('Yesterday'),
    message({ id: 201, user: ENGINEER, createdAt: '2026-10-16T22:10:00', content: 'late night' }),
    message({ id: 202, user: ME, createdAt: '2026-10-16T22:11:00', content: 'go to bed' }),
].join('\n');

async function chatEnv({ rows = yesterdayRows, ...opts } = {}) {
    const env = await createPage({ html: chatPage({ pane: conversationPane({ rows }) }), ...opts });
    await env.openSocket();
    return env;
}

const listChildren = (env) => env.$$('#message-list > *').map((el) => (
    el.classList.contains('date-separator') ? `divider:${el.textContent.trim()}` : el.id
));

describe('timestamp localization', () => {
    it('runs in a zone with a non-zero UTC offset (sanity check for this suite)', async () => {
        const env = await chatEnv();
        expect(env.eval("new Date('2026-10-17T18:42:00Z').getTimezoneOffset()")).toBe(240);
    });

    it('localizes server-rendered timestamps on page load', async () => {
        const env = await chatEnv({ rows: message({ id: 1, user: ENGINEER, createdAt: UTC_AFTERNOON }) });
        const time = env.$('#message-1 .local-time');
        expect(time.textContent).toBe(LOCAL_AFTERNOON);
        expect(time.classList.contains('time-processed')).toBe(true);
    });

    it('localizes a message appended by a realtime frame', async () => {
        const env = await chatEnv();
        await env.frame(messageFrame({ id: 300, user: ENGINEER, createdAt: UTC_AFTERNOON }));
        const time = env.$('#message-300 .local-time');
        // Regression: live messages used to stay in UTC ("06:42 PM").
        expect(time.textContent).toBe(LOCAL_AFTERNOON);
    });

    it('localizes every message of a multi-message frame, not just the last', async () => {
        const env = await chatEnv();
        await env.frame(
            messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' })
            + messageFrame({ id: 301, user: ME, createdAt: '2026-10-17T13:01:00' })
            + messageFrame({ id: 302, user: ENGINEER, createdAt: '2026-10-17T13:02:00' }),
        );
        expect(env.$$('#message-list .local-time').slice(-3).map((t) => t.textContent))
            .toEqual(['09:00 AM', '09:01 AM', '09:02 AM']);
    });

    it('does not re-localize a timestamp that was already processed', async () => {
        const env = await chatEnv({ rows: message({ id: 1, user: ENGINEER, createdAt: UTC_AFTERNOON }) });
        const time = env.$('#message-1 .local-time');
        time.textContent = 'sentinel';
        await env.frame(messageFrame({ id: 2, user: ENGINEER, createdAt: UTC_AFTERNOON }));
        expect(time.textContent).toBe('sentinel');
    });

    it('localizes a conversation pane fetched with hx-get (afterSettle path)', async () => {
        const env = await createPage({
            html: chatPage(), // #chat-messages-container keeps hx-trigger="load"
            xhrRoutes: [{
                match: `/chat/dm/${ME.id}`,
                reply: {
                    body: conversationPane({
                        convIdStr: 'dm_1_1',
                        rows: message({ id: 7, user: ME, createdAt: UTC_AFTERNOON }),
                    }),
                },
            }],
        });
        await env.tick(100);
        expect(env.$('#message-7 .local-time').textContent).toBe(LOCAL_AFTERNOON);
    });

    it('localizes a live thread reply', async () => {
        const env = await chatEnv({ rows: message({ id: 50, user: ENGINEER, createdAt: '2026-10-17T12:00:00' }) });
        env.$('#right-panel-body').innerHTML = `
            <div class="flex-grow-1" style="overflow-y: auto">
                <div id="thread-replies-list-50"></div>
            </div>`;
        await env.frame(oobToSelector('beforeend', '#thread-replies-list-50',
            message({ id: 51, user: ME, createdAt: UTC_AFTERNOON })));
        expect(env.$('#message-51 .local-time').textContent).toBe(LOCAL_AFTERNOON);
    });

    // Regression (fixed): edits arrive as message.html with hx-swap-oob="true" (outerHTML).
    // htmx's htmx:oobAfterSwap detail.target is the *replaced* element, which
    // is already detached, so chat.js's "edited messages received via
    // WebSocket" branch localizes (and re-inits popovers/highlights on) the
    // old node. The edited message's time reverts to UTC until a reload.
    it('localizes the timestamp of a message edited by someone else (OOB outerHTML)', async () => {
        const env = await chatEnv({ rows: message({ id: 60, user: ENGINEER, createdAt: UTC_AFTERNOON, content: 'tpyo' }) });
        expect(env.$('#message-60 .local-time').textContent).toBe(LOCAL_AFTERNOON);
        const edited = message({ id: 60, user: ENGINEER, createdAt: UTC_AFTERNOON, content: 'typo' })
            .replace('id="message-60"', 'id="message-60" hx-swap-oob="true"');
        await env.frame(edited);
        expect(env.$('#message-60 .message-content').textContent.trim()).toBe('typo');
        expect(env.$('#message-60 .local-time').textContent).toBe(LOCAL_AFTERNOON);
    });
});

describe('date dividers on realtime appends', () => {
    it("inserts the server's divider right above the first message of a new day", async () => {
        const env = await chatEnv();
        await env.frame(messageFrame(
            { id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' },
            { dividerLabel: 'Today' },
        ));
        expect(listChildren(env)).toEqual([
            'divider:Yesterday', 'message-201', 'message-202', 'divider:Today', 'message-300',
        ]);
    });

    it('adds no divider for a same-day message', async () => {
        const env = await chatEnv();
        await env.frame(messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }, { dividerLabel: 'Today' }));
        await env.frame(messageFrame({ id: 301, user: ME, createdAt: '2026-10-17T13:01:00' }));
        expect(listChildren(env).filter((c) => c.startsWith('divider:'))).toEqual(['divider:Yesterday', 'divider:Today']);
        expect(listChildren(env).at(-1)).toBe('message-301');
    });

    // Regression (fixed): a first-of-day message delivered twice (live broadcast + reconnect
    // backfill) carries its divider both times. htmx swaps the two OOB parts
    // one after the other, so the second divider lands *after* the original
    // message and the deduped message copy is then removed:
    //   [Today, message-300, Today]
    // chat.js only drops a divider whose previousElementSibling is an identical
    // divider, which never happens here (its neighbour is message-300), so a
    // second "Today" stays on screen above the next message. This is the case
    // commit e7b0f9e set out to handle.
    it('collapses the duplicate divider left behind when the same first-of-day message arrives twice', async () => {
        const env = await chatEnv();
        const first = messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }, { dividerLabel: 'Today' });
        await env.frame(first); // live broadcast
        await env.frame(first); // ...and again via the reconnect backfill
        await env.frame(messageFrame({ id: 301, user: ME, createdAt: '2026-10-17T13:05:00' }));
        expect(listChildren(env)).toEqual([
            'divider:Yesterday', 'message-201', 'message-202', 'divider:Today', 'message-300', 'message-301',
        ]);
    });

    it('collapses two identical dividers that end up next to each other', async () => {
        const env = await chatEnv({ rows: '' });
        await env.frame(oobToSelector('beforeend', '#message-list', dateSeparator('Today')));
        await env.frame(oobToSelector('beforeend', '#message-list', dateSeparator('Today')));
        expect(listChildren(env)).toEqual(['divider:Today']);
    });

    it('keeps adjacent dividers with different labels', async () => {
        const env = await chatEnv({ rows: '' });
        await env.frame(oobToSelector('beforeend', '#message-list', dateSeparator('Yesterday')));
        await env.frame(oobToSelector('beforeend', '#message-list', dateSeparator('Today')));
        expect(listChildren(env)).toEqual(['divider:Yesterday', 'divider:Today']);
    });
});

describe('dedupe of messages delivered twice', () => {
    it('keeps the first copy of a message that arrives twice', async () => {
        const env = await chatEnv();
        const frame = messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00', content: 'once' });
        await env.frame(frame);
        const firstCopy = env.$('#message-300');
        await env.frame(frame);
        expect(env.$$('#message-list #message-300')).toHaveLength(1);
        expect(env.$('#message-300')).toBe(firstCopy);
    });
});

describe('reactions', () => {
    // Regression (fixed): reaction updates are broadcast as one rendering of reactions.html
    // made for the *reactor* (`user-reacted` is decided server-side from
    // g.user) and swapped into #reactions-container-<id> with innerHTML. chat.js
    // only recomputes highlights for message-container / message-list OOB
    // targets, so everyone else sees the reactor's pill highlighted as if they
    // had reacted themselves, until they reload.
    it("does not highlight a reaction pill for a reaction someone else made", async () => {
        const env = await chatEnv({ rows: message({ id: 70, user: ME, createdAt: '2026-10-17T12:00:00' }) });
        await env.frame(`<div id="reactions-container-70" hx-swap-oob="innerHTML">
            <button class="btn btn-sm reaction-pill user-reacted" data-reactor-ids="${ENGINEER.id}"
                    hx-post="/chat/message/70/react" hx-vals='{"emoji": "👍"}' hx-swap="innerHTML">
                <span>👍</span><span class="reaction-count">1</span>
            </button></div>`);
        expect(env.$('#reactions-container-70 .reaction-pill').classList.contains('user-reacted')).toBe(false);
    });

    it('highlights my own reactions on a realtime-appended message', async () => {
        const env = await chatEnv();
        const html = message({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }).replace(
            '<div id="reactions-container-300" class="mt-1 d-flex flex-wrap gap-1"></div>',
            `<div id="reactions-container-300" class="mt-1 d-flex flex-wrap gap-1">
                <button class="btn btn-sm reaction-pill" data-reactor-ids="${ENGINEER.id},${ME.id}"><span>🎉</span></button>
                <button class="btn btn-sm reaction-pill user-reacted" data-reactor-ids="${ENGINEER.id}"><span>👀</span></button>
            </div>`,
        );
        await env.frame(oobToSelector('beforeend', '#message-list', html));
        const pills = env.$$('#message-300 .reaction-pill');
        expect(pills.map((p) => p.classList.contains('user-reacted'))).toEqual([true, false]);
    });
});

describe('scrolling and Jump to Latest', () => {
    function scrolledUp(env) {
        const container = env.$('#chat-messages-container');
        Object.defineProperty(container, 'scrollHeight', { configurable: true, value: 3000 });
        Object.defineProperty(container, 'clientHeight', { configurable: true, value: 600 });
        container.scrollTop = 0;
        return container;
    }

    it("shows Jump to Latest when someone else's message arrives while I'm scrolled up", async () => {
        const env = await chatEnv();
        scrolledUp(env);
        await env.frame(messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }));
        expect(env.$('#jump-to-bottom-btn').style.display).toBe('block');
    });

    it('scrolls to my own new message instead of showing Jump to Latest', async () => {
        const env = await chatEnv();
        scrolledUp(env);
        let scrolled = 0;
        env.window.Element.prototype.scrollIntoView = function scrollIntoView() {
            if (this.id === 'message-list-bottom') scrolled++;
        };
        await env.frame(messageFrame({ id: 300, user: ME, createdAt: '2026-10-17T13:00:00' }));
        await env.tick(0);
        expect(env.$('#jump-to-bottom-btn').style.display).toBe('none');
        expect(scrolled).toBeGreaterThan(0);
    });

    it("follows new messages when I'm already at the bottom", async () => {
        const env = await chatEnv();
        let scrolled = 0;
        env.window.Element.prototype.scrollIntoView = function scrollIntoView() {
            if (this.id === 'message-list-bottom') scrolled++;
        };
        await env.frame(messageFrame({ id: 300, user: ENGINEER, createdAt: '2026-10-17T13:00:00' }));
        await env.tick(0);
        expect(env.$('#jump-to-bottom-btn').style.display).toBe('none');
        expect(scrolled).toBeGreaterThan(0);
    });
});

describe('date dividers in the viewer\'s timezone', () => {
    // The suite runs in America/New_York (UTC-4 on these dates); "now" is
    // 2026-10-17T14:00Z, 10 AM local.
    it('files an evening message under its local day, not the next UTC day', async () => {
        const env = await chatEnv();
        // 02:00 UTC on the 17th is 10 PM on the 16th here. The server labelled
        // it by UTC day ("Today"); it belongs with yesterday's messages.
        await env.frame(messageFrame(
            { id: 300, user: ENGINEER, createdAt: '2026-10-17T02:00:00', content: 'still up?' },
            { dividerLabel: 'Today' },
        ));
        expect(listChildren(env)).toEqual(['divider:Yesterday', 'message-201', 'message-202', 'message-300']);
    });

    it('relabels server dividers on load and merges the seam of two pages', async () => {
        const rows = [
            dateSeparator('October 15th'),
            message({ id: 101, user: ENGINEER, createdAt: '2026-10-16T01:30:00', content: 'utc 16th, local 15th' }),
            dateSeparator('October 16th'),
            message({ id: 102, user: ENGINEER, createdAt: '2026-10-16T03:30:00', content: 'local 15th too' }),
            dateSeparator('Today'),
            message({ id: 103, user: ME, createdAt: '2026-10-17T13:00:00', content: 'morning' }),
        ].join('\n');
        const env = await createPage({ html: chatPage({ pane: conversationPane({ rows }) }) });
        await env.openSocket();
        env.eval("DateDividers.rebuild(document.getElementById('message-list'))");
        expect(listChildren(env)).toEqual([
            'divider:October 15th', 'message-101', 'message-102', 'divider:Today', 'message-103',
        ]);
    });

    it('keeps the server dividers when no message is stamped', async () => {
        const env = await chatEnv({ rows: '' });
        await env.frame(oobToSelector('beforeend', '#message-list', dateSeparator('Today')));
        expect(listChildren(env)).toEqual(['divider:Today']);
    });

    it('labels days beyond yesterday with the date, and the year when it differs', async () => {
        const env = await chatEnv();
        expect(env.eval("DateDividers.label(new Date('2026-10-03T15:00:00Z'))")).toBe('October 3rd');
        expect(env.eval("DateDividers.label(new Date('2025-12-22T15:00:00Z'))")).toBe('December 22nd, 2025');
        expect(env.eval("DateDividers.label(new Date('2026-10-11T15:00:00Z'))")).toBe('October 11th');
    });
});
