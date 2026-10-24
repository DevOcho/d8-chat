// HTML fixtures mirroring the Jinja templates the scripts run against.
//
// Each builder names the template it mirrors. Keep ids, classes and data-*
// attributes in step with the templates: fixtures-drift.test.js checks that
// the hooks these builders rely on still exist in the template sources.

export const ME = {
    id: 1,
    username: 'kp',
    displayName: 'Kenny',
    presence: 'online', // g.user.presence_status
    notificationSound: 'd8-notification.mp3',
    sendOnEnter: true,
};

export const ENGINEER = { id: 2, username: 'engineer', displayName: 'Eng Ineer' };
export const ROBERT = { id: 3, username: 'robert', displayName: 'Robert' };
export const EMILIO = { id: 4, username: 'emilio', displayName: null };

const esc = (s) => String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

export const dmConvId = (a, b) => `dm_${Math.min(a, b)}_${Math.max(a, b)}`;
const name = (u) => u.displayName || u.username;

// Python's strftime("%I:%M %p") on a naive-UTC datetime: the server fallback
// text inside .local-time before the browser localizes it.
function utcClock(iso) {
    const d = new Date(iso);
    const h = d.getUTCHours();
    const h12 = h % 12 === 0 ? 12 : h % 12;
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    return `${String(h12).padStart(2, '0')}:${mm} ${h < 12 ? 'AM' : 'PM'}`;
}

// partials/_avatar_display.html (no avatar_url → initial fallback)
export function avatar(user, size = 40) {
    if (user.avatarUrl) {
        return `<img src="${esc(user.avatarUrl)}" alt="${esc(user.username)}'s avatar" class="rounded-circle avatar-image" data-user-id="${user.id}" style="width: ${size}px; height: ${size}px; object-fit: cover">`;
    }
    return `<div class="rounded-circle bg-secondary text-white d-flex align-items-center justify-content-center avatar-image" data-user-id="${user.id}" style="width: ${size}px; height: ${size}px; font-size: ${(size / 24) * 0.8}rem">${esc(name(user)[0].toUpperCase())}</div>`;
}

// partials/date_separator.html
export function dateSeparator(label) {
    return `<div class="date-separator">\n    <span>${esc(label)}</span>\n</div>`;
}

// partials/message_toolbar.html (the hooks chat.js uses)
function messageToolbar(id) {
    return `<div class="message-toolbar">
        <button class="btn btn-sm btn-light" data-bs-toggle="popover" data-message-id="${id}" title="Add Reaction"><i class="bi bi-emoji-smile"></i></button>
        <button class="btn btn-sm btn-light" hx-get="/chat/message/${id}/reply" hx-target="#chat-input-container" hx-swap="outerHTML" data-action="reply"><i class="bi bi-reply"></i></button>
        <button class="btn btn-sm btn-light author-only-action" hx-get="/chat/message/${id}/load_for_edit" hx-target="#message-${id}" hx-swap="outerHTML" data-action="edit"><i class="bi bi-pencil"></i></button>
    </div>`;
}

/**
 * partials/message.html for a user message.
 *
 * @param {object} m
 * @param {number} m.id
 * @param {object} m.user
 * @param {string} m.createdAt   naive-UTC ISO as Python's isoformat() prints it
 * @param {string} [m.content]
 * @param {boolean} [m.sameSender]   grouped under the previous message
 * @param {boolean} [m.mentioned]    message.id in mention_message_ids
 * @param {string} [m.clientId]      the sending tab's id, echoed for its optimistic copy
 */
export function message({ id, user, createdAt, content = 'hello', sameSender = false, mentioned = false, clientId = null }) {
    const header = sameSender ? '' : `
            <div>
                <strong class="me-2">${esc(name(user))}</strong>
                <small class="text-muted local-time" data-timestamp="${createdAt}Z">${utcClock(`${createdAt}Z`)}</small>
            </div>`;
    const avatarCol = sameSender
        ? '<div class="flex-shrink-0" style="width: 40px; height: 40px;"></div>'
        : `<div class="flex-shrink-0" style="cursor: pointer" hx-get="/chat/dm/${user.id}/details?context=channel" hx-target="#right-panel-offcanvas" title="View user details">${avatar(user, 40)}</div>`;
    return `<div class="d-flex mb-1 message-container ${sameSender ? 'grouped-message' : ''} ${mentioned ? 'mentioned-message' : ''}"
         id="message-${id}"
         data-user-id="${user.id}"
         ${clientId ? `data-client-id="${esc(clientId)}"` : ''}
         ${createdAt ? `data-created-at="${createdAt}Z"` : ''}
         data-raw-content="${esc(content)}"
         data-attachments='[]'>
        ${avatarCol}
        <div class="flex-grow-1 ms-3">${header}
            <div class="message-content ">
                <p>${esc(content)}</p>
            </div>
            <div id="reactions-container-${id}" class="mt-1 d-flex flex-wrap gap-1"></div>
        </div>
        ${messageToolbar(id)}
    </div>`;
}

// partials/message.html, system-announcement branch
export function systemMessage({ id, content, createdAt = null }) {
    const stamp = createdAt ? ` data-created-at="${createdAt}Z"` : '';
    return `<div class="system-message" id="message-${id}"${stamp}>${esc(content)}</div>`;
}

// app/htmx_oob.py oob_to_selector()
export function oobToSelector(swap, selector, inner) {
    return `<div hx-swap-oob="${swap}:${esc(selector)}">${inner}</div>`;
}

// app/htmx_oob.py message_list_append_oob(); convIdStr null = an untagged
// append from a server that predates the tag.
export function messageListAppend(convIdStr, inner) {
    if (convIdStr === null) return oobToSelector('beforeend', '#message-list', inner);
    return `<div hx-swap-oob="beforeend:#message-list" data-conversation-id="${esc(convIdStr)}">${inner}</div>`;
}

/**
 * The realtime frame routes._broadcast_regular_message sends a web client:
 * date_separator_oob() (only when the message opens a new day) followed by the
 * message appended to #message-list, both tagged with their conversation.
 */
export function messageFrame(m, { dividerLabel = null, convIdStr = 'channel_1' } = {}) {
    const divider = dividerLabel ? messageListAppend(convIdStr, dateSeparator(dividerLabel)) : '';
    return divider + messageListAppend(convIdStr, message(m));
}

/**
 * partials/channel_messages.html (dm_messages.html has the same hooks).
 * `rows` is pre-rendered list content (date separators + messages).
 */
export function conversationPane({ convIdStr = 'channel_1', dbId = 10, rows = '' } = {}) {
    return `<div class="h-100"
     data-conversation-id="${convIdStr}"
     data-conversation-db-id="${dbId}">
    <div ws-send
         hx-trigger="load"
         hx-vals='{"type": "subscribe", "conversation_id": "${convIdStr}"}'></div>
    <div id="message-list">
${rows}
    </div>
    <div id="message-list-bottom"></div>
</div>`;
}

// partials/unread_badge.html
export function unreadBadge({ convIdStr, count, linkText, hxGetUrl }) {
    return `<span hx-swap-oob="innerHTML:#unread-badge-${convIdStr}">
    <span class="badge rounded-pill bg-danger">${count}</span>
</span>
<a hx-swap-oob="outerHTML:#link-${convIdStr}"
   id="link-${convIdStr}"
   href="#"
   class="text-decoration-none fw-bold text-white"
   hx-target="#chat-messages-container"
   hx-get="${hxGetUrl}">${esc(linkText)}</a>`;
}

// partials/bold_link.html
export function boldLink({ convIdStr, linkText, hxGetUrl }) {
    return `<a hx-swap-oob="outerHTML:#link-${convIdStr}"
   id="link-${convIdStr}"
   href="#"
   class="text-decoration-none fw-bold text-white"
   hx-target="#chat-messages-container"
   hx-get="${hxGetUrl}">${esc(linkText)}</a>`;
}

// partials/clear_badge.html
export function clearBadge({ convIdStr, linkText, hxGetUrl }) {
    return `<span id="unread-badge-${convIdStr}" hx-swap-oob="outerHTML"></span>
<a hx-swap-oob="outerHTML:#link-${convIdStr}"
   id="link-${convIdStr}"
   href="#"
   class="text-decoration-none text-secondary"
   hx-target="#chat-messages-container"
   hx-get="${hxGetUrl}">${esc(linkText)}</a>`;
}

// partials/unreads_link_unread.html / unreads_link_read.html
export function unreadsLink({ unread }) {
    const cls = unread ? 'text-decoration-none fw-bold text-white' : 'text-decoration-none text-light text-opacity-75';
    return `<a href="#"
   id="unreads-link"
   class="${cls}"
   hx-get="/chat/unreads"
   hx-target="#chat-messages-container"
   hx-swap="innerHTML"
   hx-swap-oob="true">
    <i class="bi bi-envelope-fill me-2" style="width: 16px; display: inline-block"></i>Unreads
</a>`;
}

/**
 * routes.broadcast_conversation_read(): the web half of the read-state sync
 * (clear_badge.html + unreads_link_read.html when nothing else is unread).
 */
export function readStateFrame({ convIdStr, linkText, hxGetUrl, nothingElseUnread = true }) {
    return clearBadge({ convIdStr, linkText, hxGetUrl }) + (nothingElseUnread ? unreadsLink({ unread: false }) : '');
}

// partials/dm_list_item_oob.html
export function dmListItemOob({ convIdStr, user, presenceClass = 'presence-away' }) {
    return `<div hx-swap-oob="beforeend:#dm-list">
    <li class="mt-1" id="dm-item-${convIdStr}">
        <a href="#"
           class="sidebar-item-link"
           hx-get="/chat/dm/${user.id}"
           hx-target="#chat-messages-container">
            <div class="d-flex justify-content-between align-items-center">
                <div class="d-flex align-items-center">
                    <div class="sidebar-avatar me-2">
                        ${avatar(user, 24)}
                        <span id="status-dot-${user.id}"
                              class="presence-indicator ${presenceClass}"></span>
                    </div>
                    <span id="link-${convIdStr}" class="text-decoration-none text-light">
                        ${esc(name(user))}
                    </span>
                </div>
                <span id="unread-badge-${convIdStr}"></span>
            </div>
        </a>
    </li>
</div>`;
}

/**
 * chat_service.send_notifications_for_new_message() for a DM: the sidebar row
 * rides along with the badge so a client missing the row can materialize it.
 */
export function dmBadgeFrame({ me = ME, sender, count }) {
    const convIdStr = dmConvId(me.id, sender.id);
    return dmListItemOob({ convIdStr, user: sender }) + unreadBadge({
        convIdStr,
        count,
        linkText: name(sender),
        hxGetUrl: `/chat/dm/${sender.id}`,
    });
}

// partials/subscribe_oob.html
export function subscribeOob(convIdStr) {
    return `<div hx-swap-oob="true"
     id="ws-subscriber-${convIdStr}"
     ws-send
     hx-vals='{"type": "subscribe", "conversation_id": "${convIdStr}"}'></div>`;
}

// partials/_sidebar_profile_button.html
function sidebarProfileButton(user) {
    const cls = user.presence === 'away' ? 'presence-away' : user.presence === 'busy' ? 'presence-busy' : 'presence-online';
    return `<a href="#" id="sidebar-profile-button" title="View your profile" hx-get="/profile" hx-target="#right-panel-offcanvas">
        <div class="profile-avatar">
            ${avatar(user, 36)}
            <span id="sidebar-presence-indicator-${user.id}" class="presence-indicator ${cls}"></span>
        </div>
    </a>`;
}

// partials/profile_header.html (the status menu initPresence follows)
export function profileHeader(user = ME) {
    const cls = user.presence === 'away' ? 'presence-away' : user.presence === 'busy' ? 'presence-busy' : 'presence-online';
    const choice = (status) => `<li>
                    <button class="dropdown-item"
                            data-presence-choice="${status}"
                            hx-put="/profile/presence"
                            hx-vals='{"status": "${status}"}'
                            hx-target="#profile-header-card"
                            hx-swap="outerHTML">
                        <span class="presence-dot presence-${status}"></span> ${status}
                    </button>
                </li>`;
    return `<div id="profile-header-card" class="card mb-4 border-0 shadow-sm">
    <div class="card-body p-3 d-flex align-items-center">
        <div class="flex-shrink-0 me-3 dropdown">
            <div id="avatar-dropdown-trigger" data-bs-toggle="dropdown" aria-expanded="false">
                <div class="profile-header-avatar">
                    ${avatar(user, 60)}
                    <span id="profile-presence-indicator-${user.id}" class="presence-indicator ${cls}"></span>
                </div>
            </div>
            <ul class="dropdown-menu" aria-labelledby="avatar-dropdown-trigger">
                ${choice('online')}
                ${choice('away')}
                ${choice('busy')}
            </ul>
        </div>
    </div>
</div>`;
}

// The two preference selects from partials/profile_details.html. Changing one
// PUTs to its route, which answers with an HX-Trigger event (preferenceReply).
export function preferenceSelects({ sendOnEnter = true, sound = 'd8-notification.mp3' } = {}) {
    return `<select class="form-select" id="send-key-select" name="send_on_enter"
                    hx-put="/profile/send_on_enter" hx-trigger="change" hx-swap="none">
                <option value="true" ${sendOnEnter ? 'selected' : ''}>Enter sends (Shift+Enter for a new line)</option>
                <option value="false" ${sendOnEnter ? '' : 'selected'}>Ctrl+Enter sends (Enter for a new line)</option>
            </select>
            <select class="form-select" id="notification-sound-select" name="sound"
                    hx-put="/profile/notification_sound" hx-trigger="change" hx-swap="none">
                <option value="d8-notification.mp3" ${sound === 'd8-notification.mp3' ? 'selected' : ''}>Default (D8)</option>
                <option value="slack-notification.mp3" ${sound === 'slack-notification.mp3' ? 'selected' : ''}>Slack</option>
            </select>`;
}

// What the preference routes in blueprints/profile.py answer: an empty body
// and an HX-Trigger header carrying the new value.
export function preferenceReply(event, value) {
    return { status: 200, body: '', headers: { 'HX-Trigger': JSON.stringify({ [event]: value }) } };
}

// partials/chat_input_default.html. chat.js initializes the editor when htmx
// loads a form marked data-editor.
export function chatInputDefault({ wysiwyg = true } = {}) {
    return `<div id="chat-input-container" class="chat-input-form">
    <div id="typing-indicator" class="text-muted small"></div>
    <form hx-post="/chat/conversations/_pending_/messages"
          hx-swap="none"
          id="message-form"
          data-editor=""
          autocomplete="off">
        <input type="file" id="file-attachment-input" class="d-none" multiple>
        <input type="hidden" name="attachment_file_ids" id="attachment-file-ids">
        <div class="wysiwyg-editor-container">
            <div id="attachment-previews" class="p-2 flex-wrap gap-2" style="display: none"></div>
            <div class="wysiwyg-toolbar toolbar-hidden">
                <button type="button" data-command="bold" title="Bold (Ctrl+B)"><i class="bi bi-type-bold"></i></button>
                <button type="button" data-command="italic" title="Italic (Ctrl+I)"><i class="bi bi-type-italic"></i></button>
                <button type="button" data-command="strikethrough" title="Strikethrough"><i class="bi bi-type-strikethrough"></i></button>
                <button type="button" data-command="insertUnorderedList" title="Bulleted List"><i class="bi bi-list-ul"></i></button>
                <button type="button" data-command="insertOrderedList" title="Numbered List"><i class="bi bi-list-ol"></i></button>
                <button type="button" data-command="formatBlock" data-value="blockquote" title="Quote"><i class="bi bi-quote"></i></button>
                <button type="button" data-command="formatBlock" data-value="pre" title="Code Block"><i class="bi bi-code-slash"></i></button>
            </div>
            <div id="wysiwyg-editor"
                 class="wysiwyg-content"
                 contenteditable="true"
                 style="display: ${wysiwyg ? 'block' : 'none'}"></div>
            <textarea id="markdown-toggle-view"
                      class="form-control"
                      rows="1"
                      style="display: ${wysiwyg ? 'none' : 'block'}"></textarea>
            <textarea id="chat-message-input" name="chat_message" style="display: none;"></textarea>
            <div class="wysiwyg-toolbar wysiwyg-toolbar-bottom">
                <button type="button" id="format-toggle-btn" title="Toggle Rich Formatting"><i class="bi bi-type"></i></button>
                <button type="button" id="file-attachment-btn" title="Attach a file"><i class="bi bi-paperclip"></i></button>
                <button type="button" id="mention-btn" title="Mention User"><i class="bi bi-at"></i></button>
                <button type="button" id="emoji-btn" title="Add Emoji"><i class="bi bi-emoji-smile"></i></button>
                <div class="ms-auto d-flex align-items-center">
                    <button id="send-button" type="submit" class="btn btn-primary send-btn-dynamic"></button>
                </div>
            </div>
        </div>
        <div id="emoji-picker-container" style="display: none">
            <emoji-picker class="light" data-source="/js/local/emoji.json"></emoji-picker>
        </div>
    </form>
    <div id="typing-sender"
         ws-send
         hx-trigger="typing-event"
         style="display: none"></div>
</div>`;
}

// partials/mention_results.html (user rows only)
export function mentionResults(users) {
    if (!users.length) return '<div class="list-group list-group-flush"><div class="list-group-item p-2 text-muted">No results found</div></div>';
    const rows = users.map((u, i) => `<button type="button"
                class="list-group-item list-group-item-action mention-suggestion-item p-2 ${i === 0 ? 'active' : ''}"
                data-username="${esc(u.username)}">
            <div class="d-flex align-items-center">
                <div class="flex-grow-1 ms-2">
                    <strong class="me-2">${esc(name(u))}</strong>
                    <br>
                    <small class="text-muted">${esc(u.username)}</small>
                </div>
            </div>
        </button>`).join('\n');
    return `<div class="list-group list-group-flush">\n${rows}\n</div>`;
}

/**
 * chat.html rendered for `user`, with the base.html shell around it.
 *
 * @param {object} o
 * @param {object} [o.user]
 * @param {Array}  [o.channels]  [{ id, name, unread, mentions }]
 * @param {Array}  [o.dms]       [{ user, presenceClass, unread, mentions }]
 * @param {string} [o.pane]      #chat-messages-container content; when given the
 *                               container's load trigger is dropped (the pane is
 *                               already "loaded"). Omit to keep the real
 *                               hx-trigger="load" fetch of /chat/dm/<me>.
 * @param {boolean} [o.inputLoader]  keep the composer's hx-trigger="load" loader
 * @param {string} [o.oncallUntil]   naive-UTC ISO → render the "You're on call" badge
 * @param {string} [o.extraMain]     extra markup appended inside <main>
 */
export function chatPage({
    user = ME,
    channels = [{ id: 1, name: 'general' }, { id: 5, name: 'devops' }],
    dms = [{ user: ENGINEER, presenceClass: 'presence-online' }],
    pane,
    inputLoader = false,
    oncallUntil = null,
    extraMain = '',
} = {}) {
    const selfPresence = user.presence === 'busy' ? 'presence-busy' : user.presence === 'away' ? 'presence-away' : 'presence-online';
    const selfConv = dmConvId(user.id, user.id);

    const channelItems = channels.map((c) => {
        const conv = `channel_${c.id}`;
        return `<li id="channel-item-${c.id}" class="mt-1">
                    <a href="#" class="sidebar-item-link" hx-get="/chat/channel/${c.id}" hx-target="#chat-messages-container">
                        <div class="d-flex justify-content-between align-items-center">
                            <span id="link-${conv}" class="text-decoration-none ${c.unread ? 'fw-bold text-white' : 'text-secondary'}">
                                # ${esc(c.name)}
                            </span>
                            <span id="unread-badge-${conv}">${c.mentions ? `<span class="badge rounded-pill bg-danger float-end">${c.mentions}</span>` : ''}</span>
                        </div>
                    </a>
                </li>`;
    }).join('\n');

    const dmItems = dms.map(({ user: other, presenceClass = 'presence-away', unread = false, mentions = 0 }) => {
        const conv = dmConvId(user.id, other.id);
        return `<li class="mt-1" id="dm-item-${conv}">
                    <a href="#" class="sidebar-item-link" hx-get="/chat/dm/${other.id}" hx-target="#chat-messages-container">
                        <div class="d-flex justify-content-between align-items-center">
                            <div class="d-flex align-items-center">
                                <div class="sidebar-avatar me-2">
                                    ${avatar(other, 24)}
                                    <span id="status-dot-${other.id}" class="presence-indicator ${presenceClass}"></span>
                                </div>
                                <span id="link-${conv}" class="text-decoration-none ${unread ? 'fw-bold text-white' : 'text-secondary'}">
                                    ${esc(name(other))}
                                </span>
                            </div>
                            <span id="unread-badge-${conv}">${mentions ? `<span class="badge rounded-pill bg-danger">${mentions}</span>` : ''}</span>
                        </div>
                    </a>
                </li>`;
    }).join('\n');

    const oncallBadge = oncallUntil ? `<div class="oncall-badge mt-2" title="You get paged for DOWN alerts until the next handoff.">
            <i class="bi bi-broadcast-pin me-1"></i>You're on call
            <div class="oncall-badge-until">
                until
                <span class="incident-time" data-format="datetime" data-timestamp="${oncallUntil}Z">${oncallUntil} UTC</span>
            </div>
        </div>` : '';

    const containerAttrs = pane === undefined
        ? `hx-get="/chat/dm/${user.id}" hx-trigger="load"`
        : `hx-get="/chat/dm/${user.id}"`;
    const paneContent = pane === undefined
        ? '<div class="text-center text-muted mt-5"><div class="spinner-border" role="status"><span class="visually-hidden">Loading your space...</span></div></div>'
        : pane;
    const composer = inputLoader
        ? `<div hx-get="/chat/input/default" hx-trigger="load" hx-swap="outerHTML">
            <div class="chat-input-form bg-light"><div class="spinner-border spinner-border-sm" role="status"></div></div>
        </div>`
        : '';

    return `<!DOCTYPE html>
<html lang="en" data-bs-theme="light">
    <head>
        <meta charset="utf-8">
        <meta name="csrf-token" content="csrf-test-token">
        <title>D8 - Chat</title>
        <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png?v=0123abcd">
        <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png?v=4567ef01">
    </head>
    <body class="">
        <div class="main-layout">
    <div class="sidebar workspace-sidebar p-2 text-center text-white">
        <div class="workspace-logo mb-3">D8</div>
        <div class="mt-auto">${sidebarProfileButton(user)}</div>
    </div>
    <div class="sidebar channel-sidebar p-3">
        <h5 class="text-white">DevOcho</h5>
        ${oncallBadge}
        <hr class="text-secondary">
        <ul id="activity-links" class="list-unstyled mb-4" hx-sync="#chat-messages-container:replace" hx-indicator="#pane-progress">
            <li>
                <a href="#" id="unreads-link" class="text-decoration-none text-light text-opacity-75" hx-get="/chat/unreads" hx-target="#chat-messages-container" hx-swap="innerHTML">Unreads</a>
            </li>
            <li>
                <a href="#" id="threads-link" class="text-decoration-none text-light text-opacity-75" hx-get="/chat/threads" hx-target="#chat-messages-container" hx-swap="innerHTML">Threads</a>
            </li>
        </ul>
        <ul id="channel-list" class="list-unstyled" hx-sync="#chat-messages-container:replace" hx-indicator="#pane-progress">
${channelItems}
        </ul>
        <ul id="dm-list" class="list-unstyled" hx-sync="#chat-messages-container:replace" hx-indicator="#pane-progress">
            <li class="mt-1" id="dm-self-item">
                <a href="#" class="sidebar-item-link" hx-get="/chat/dm/${user.id}" hx-target="#chat-messages-container">
                    <div class="d-flex justify-content-between align-items-center">
                        <div class="d-flex align-items-center">
                            <div class="sidebar-avatar me-2">
                                ${avatar(user, 24)}
                                <span id="status-dot-${user.id}" class="presence-indicator ${selfPresence}"></span>
                            </div>
                            <span id="link-${selfConv}" class="text-decoration-none text-light">${esc(name(user))} (you)</span>
                        </div>
                        <span id="unread-badge-${selfConv}"></span>
                    </div>
                </a>
            </li>
${dmItems}
        </ul>
    </div>
    <main class="main-content"
          ws-connect="/ws/chat"
          hx-ext="ws"
          data-notification-sound="${user.notificationSound}"
          data-send-on-enter="${user.sendOnEnter ? 'true' : 'false'}"
          data-current-user-id="${user.id}"
          data-current-user-username="${user.username}"
          data-current-user-display-name="${esc(name(user))}"
          data-current-user-status="${user.presence}"
          data-activity-url="/profile/activity">
        <div id="global-search-container" class="p-2 border-bottom">
            <div class="global-search-wrapper">
                <form id="global-search-form" hx-target="#search-results-overlay" hx-swap="innerHTML">
                    <input type="search" id="global-search-input" name="q" class="form-control" autocomplete="off"
                           hx-get="/chat/search" hx-trigger="keyup changed delay:300ms, search"
                           hx-target="#search-results-overlay" hx-swap="innerHTML">
                </form>
            </div>
        </div>
        <div id="pane-progress" class="pane-progress" aria-hidden="true"></div>
        <div id="connection-status-bar"
             class="p-2 bg-warning text-dark text-center"
             style="display: none; flex-shrink: 0">
            <div class="spinner-border spinner-border-sm me-2" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            Connection lost. Attempting to reconnect...
        </div>
        <div id="chat-header-container" class="p-3 border-bottom bg-white" style="flex-shrink: 0"></div>
        <div id="chat-messages-container"
             class="chat-messages position-relative"
             style="flex-grow: 1; overflow-y: auto"
             ${containerAttrs}>
            ${paneContent}
        </div>
        <div class="jump-to-latest-anchor">
            <button id="jump-to-bottom-btn" type="button" class="btn btn-primary btn-sm rounded-pill shadow" style="display: none">
                <span class="jump-label">New messages</span> <i class="bi bi-arrow-down" aria-hidden="true"></i>
            </button>
        </div>
        ${composer}
        <div id="pager-overlay" class="pager-overlay" role="alertdialog" aria-modal="true" aria-labelledby="pager-heading" hidden>
            <div class="pager-panel">
                <div class="pager-heading" id="pager-heading">You are being paged</div>
                <div id="pager-sound-hint" class="pager-sound-hint" hidden>Your browser blocked the alarm sound. Click anywhere to start it.</div>
                <div id="pager-incidents"></div>
                <div class="pager-footer">Acknowledging stops the alarm for everyone and tells the team you're on it.</div>
            </div>
        </div>
        <div id="search-results-overlay" style="display: none; flex-direction: column"></div>
        <div class="offcanvas offcanvas-end" tabindex="-1" id="right-panel-offcanvas">
            <div class="offcanvas-body" id="right-panel-body"></div>
        </div>
        <div id="quick-switcher" class="quick-switcher" role="dialog" aria-modal="true" aria-labelledby="quick-switcher-title" hidden>
            <div class="quick-switcher-panel shadow-lg">
                <h2 id="quick-switcher-title" class="visually-hidden">Jump to a conversation</h2>
                <input type="text" id="quick-switcher-input" class="form-control form-control-lg" role="combobox" aria-expanded="true" autocomplete="off">
                <ul id="quick-switcher-results" class="list-group list-group-flush" role="listbox"></ul>
            </div>
        </div>
        <div class="modal fade" id="confirm-modal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog"><div class="modal-content">
                <h2 class="modal-title" id="confirm-modal-title">Are you sure?</h2>
                <div class="modal-body" id="confirm-modal-body"></div>
                <button type="button" class="btn btn-light" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-danger" id="confirm-modal-ok">Confirm</button>
            </div></div>
        </div>
        <div class="modal fade" id="htmx-modal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-lg"><div class="modal-content" id="htmx-modal-content"></div></div>
        </div>
        <div id="enable-notifications-banner" class="bg-primary text-white text-center p-2 position-fixed bottom-0 start-0 w-100 shadow-lg" style="z-index: 1060; display: none">
            <button id="enable-notifications-btn" class="btn btn-light btn-sm fw-bold">Enable Notifications</button>
        </div>
        ${extraMain}
    </main>
        </div>
        <div class="toast-container position-fixed bottom-0 end-0 p-3">
            <div id="app-toast" class="toast" role="alert" aria-live="assertive" aria-atomic="true">
                <div id="toast-header" class="toast-header bg-danger text-white">
                    <strong id="toast-title" class="me-auto">An Error Occurred</strong>
                </div>
                <div class="toast-body" id="toast-body-content"></div>
            </div>
        </div>
    </body>
</html>`;
}

// --- On-call (oncall_service.serialize_incident / page frames) ---------------

export function incident(overrides = {}) {
    return {
        id: 42,
        title: 'api.devocho.com',
        details: 'Connection refused',
        source: 'uptime_kuma',
        source_url: 'https://api.devocho.com/health',
        state: 'triggered',
        current_level: 1,
        triggered_at: '2026-10-17T13:58:00',
        next_escalation_at: '2026-10-17T14:03:00',
        acknowledged_at: null,
        acknowledged_by: null,
        resolved_at: null,
        resolved_by: null,
        channel_id: 5,
        channel_name: 'devops',
        conversation_id_str: 'channel_5',
        message_id: 900,
        paged_users: [{ id: 1, username: 'kp', display_name: 'Kenny', level: 1 }],
        ...overrides,
    };
}

// oncall_service.page_frame()
export function pageFrame(inc = incident(), { repeat = false } = {}) {
    return { type: 'page', repeat, incident: inc };
}

// oncall_service._deliver_page_cleared()
export function pageClearedFrame(inc = incident(), state = 'acknowledged') {
    return { type: 'page_cleared', incident_id: inc.id, state, incident: { ...inc, state } };
}

// oncall_service.send_test_page()
export function testPageFrame(id = 'test-1792245600') {
    return {
        type: 'page',
        test: true,
        repeat: false,
        incident: {
            id,
            title: 'Test page',
            details: 'This is a test of your D8-Chat pager. Nothing is down.',
            source: 'test',
            source_url: null,
            state: 'triggered',
            current_level: 1,
            triggered_at: '2026-10-17T14:00:00',
            channel_name: null,
            conversation_id_str: null,
            message_id: null,
            paged_users: [{ id: 1, username: 'kp', display_name: 'Kenny', level: 1 }],
        },
    };
}
