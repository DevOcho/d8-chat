// Sidebar state driven by realtime frames: unread badges and bold links,
// the read-state sync that clears them (broadcast_conversation_read), the
// Unreads link, the DM sidebar self-heal, and the favicon unread count.
import { describe, expect, it } from 'vitest';
import { createPage } from './helpers/env.js';
import {
    EMILIO, ENGINEER, ME, ROBERT, boldLink, chatPage, conversationPane, dmBadgeFrame, dmConvId,
    dmListItemOob, readStateFrame, subscribeOob, unreadBadge, unreadsLink,
} from './helpers/fixtures.js';

async function chatEnv(page = {}) {
    const env = await createPage({
        html: chatPage({ pane: conversationPane({ convIdStr: 'channel_1' }), ...page }),
    });
    await env.openSocket();
    return env;
}

const DEVOPS = { convIdStr: 'channel_5', linkText: '# devops', hxGetUrl: '/chat/channel/5' };
const badgeText = (env, conv) => env.$(`#unread-badge-${conv}`)?.textContent.trim();
const isBold = (env, conv) => env.$(`#link-${conv}`).classList.contains('fw-bold');
const unreadCount = (env) => env.eval('FaviconManager.calculateUnreadCount()');

describe('unread badges', () => {
    it('shows the mention count and bolds the channel link', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }));
        expect(badgeText(env, 'channel_5')).toBe('2');
        expect(isBold(env, 'channel_5')).toBe(true);
        // The new link still opens the channel.
        expect(env.$('#link-channel_5').getAttribute('hx-get')).toBe('/chat/channel/5');
    });

    it('updates the count in place on the next mention', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }));
        await env.frame(unreadBadge({ ...DEVOPS, count: 3 }));
        expect(env.$$('#unread-badge-channel_5 .badge').map((b) => b.textContent)).toEqual(['3']);
    });

    it('bolds a channel without a badge for a plain (non-mention) message', async () => {
        const env = await chatEnv();
        await env.frame(boldLink(DEVOPS));
        expect(isBold(env, 'channel_5')).toBe(true);
        expect(badgeText(env, 'channel_5')).toBe('');
    });

    it('bolds the Unreads link', async () => {
        const env = await chatEnv();
        await env.frame(unreadsLink({ unread: true }));
        expect(env.$('#unreads-link').classList.contains('fw-bold')).toBe(true);
    });

    it('counts sidebar badges for the favicon', async () => {
        const env = await chatEnv();
        expect(unreadCount(env)).toBe(0);
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }));
        await env.frame(dmBadgeFrame({ sender: ENGINEER, count: 4 }));
        expect(unreadCount(env)).toBe(6);
    });
});

describe('read-state sync (unread_updated, web half)', () => {
    it('clears the badge, un-bolds the link and quiets the Unreads link', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }) + unreadsLink({ unread: true }));
        await env.frame(readStateFrame(DEVOPS));
        expect(badgeText(env, 'channel_5')).toBe('');
        expect(isBold(env, 'channel_5')).toBe(false);
        expect(env.$('#unreads-link').classList.contains('fw-bold')).toBe(false);
        expect(unreadCount(env)).toBe(0);
    });

    it('leaves the Unreads link bold when other conversations are still unread', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }) + unreadsLink({ unread: true }));
        await env.frame(readStateFrame({ ...DEVOPS, nothingElseUnread: false }));
        expect(badgeText(env, 'channel_5')).toBe('');
        expect(env.$('#unreads-link').classList.contains('fw-bold')).toBe(true);
    });

    it('clears a DM badge', async () => {
        const env = await chatEnv();
        const conv = dmConvId(ME.id, ENGINEER.id);
        await env.frame(dmBadgeFrame({ sender: ENGINEER, count: 1 }));
        expect(badgeText(env, conv)).toBe('1');
        await env.frame(readStateFrame({ convIdStr: conv, linkText: 'Eng Ineer', hxGetUrl: `/chat/dm/${ENGINEER.id}` }));
        expect(badgeText(env, conv)).toBe('');
        expect(isBold(env, conv)).toBe(false);
    });

    it('a badge can come back after it was cleared', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 1 }));
        await env.frame(readStateFrame(DEVOPS));
        await env.frame(unreadBadge({ ...DEVOPS, count: 1 }));
        expect(badgeText(env, 'channel_5')).toBe('1');
    });

    it('ignores the mobile-only JSON payloads without touching the DOM', async () => {
        const env = await chatEnv();
        await env.frame(unreadBadge({ ...DEVOPS, count: 2 }));
        const before = env.$('#channel-list').innerHTML;
        await env.frame({ type: 'unread_updated', data: { conversation_id_str: 'channel_5', unread_count: 0, is_mention: false } });
        // What web clients used to receive for an api_data-only payload.
        await env.frame({});
        expect(env.$('#channel-list').innerHTML).toBe(before);
    });
});

describe('DM sidebar self-heal', () => {
    it("materializes the sidebar row for a DM that isn't in the sidebar yet, with its badge", async () => {
        const env = await chatEnv({ dms: [] });
        const conv = dmConvId(ME.id, ROBERT.id);
        expect(env.$(`#dm-item-${conv}`)).toBeNull();
        await env.frame(dmBadgeFrame({ sender: ROBERT, count: 1 }));
        expect(env.$(`#dm-item-${conv}`)).not.toBeNull();
        expect(badgeText(env, conv)).toBe('1');
        expect(isBold(env, conv)).toBe(true);
        expect(env.$(`#link-${conv}`).textContent.trim()).toBe('Robert');
    });

    it('does not duplicate the row when it already exists', async () => {
        const env = await chatEnv({ dms: [{ user: ENGINEER, presenceClass: 'presence-online' }] });
        const conv = dmConvId(ME.id, ENGINEER.id);
        const row = env.$(`#dm-item-${conv}`);
        await env.frame(dmBadgeFrame({ sender: ENGINEER, count: 1 }));
        await env.frame(dmBadgeFrame({ sender: ENGINEER, count: 2 }));
        expect(env.$$(`[id="dm-item-${conv}"]`)).toHaveLength(1);
        expect(env.$(`#dm-item-${conv}`)).toBe(row);
        // The existing presence dot isn't reset to the row's default.
        expect(env.$(`#status-dot-${ENGINEER.id}`).classList.contains('presence-online')).toBe(true);
        expect(badgeText(env, conv)).toBe('2');
    });

    it('adds the row once for a dm_created frame and again-delivered badges', async () => {
        const env = await chatEnv({ dms: [] });
        const conv = dmConvId(ME.id, EMILIO.id);
        // dms.py: new_contact_html + subscription_html for the recipient.
        await env.frame(dmListItemOob({ convIdStr: conv, user: EMILIO }) + subscribeOob(conv));
        await env.frame(dmBadgeFrame({ sender: EMILIO, count: 1 }));
        expect(env.$$(`[id="dm-item-${conv}"]`)).toHaveLength(1);
        expect(env.$(`#link-${conv}`).textContent.trim()).toBe('emilio');
        expect(badgeText(env, conv)).toBe('1');
    });

    // Note: subscribe_oob.html targets #ws-subscriber-<conv>, which no page
    // renders, so htmx drops it (htmx:oobErrorNoTarget). htmx 2 reports that
    // with detail.content, not detail.target, so chat.js's "UI Sync Error"
    // toast never fires; the drop is silent.
    it('silently drops OOB fragments whose target is not on the page', async () => {
        const env = await chatEnv({ dms: [] });
        const conv = dmConvId(ME.id, EMILIO.id);
        await env.frame(dmListItemOob({ convIdStr: conv, user: EMILIO }) + subscribeOob(conv));
        expect(env.$(`#dm-item-${conv}`)).not.toBeNull();
        expect(env.$(`#ws-subscriber-${conv}`)).toBeNull();
        expect(env.toasts).toEqual([]);
    });
});
