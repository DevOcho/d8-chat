// Guards the fixtures against template drift.
//
// The JS tests run against hand-written copies of the Jinja markup
// (helpers/fixtures.js). If a template renames an id/class/attribute the
// scripts depend on, those copies would keep the JS tests green while the real
// page breaks. Each hook below is something chat.js / pager.js / htmx-ws.js
// (or a fixture standing in for a server render) relies on; this fails as soon
// as one disappears from its template or route, pointing at the fixture to
// update alongside it.
import { describe, expect, it } from 'vitest';
import { readSource } from './helpers/env.js';

const HOOKS = {
    'app/templates/base.html': [
        '<meta name="csrf-token"',
        'sizes="32x32"',
        'id="app-toast"',
        'id="toast-header"',
        'id="toast-title"',
        'id="toast-body-content"',
    ],
    'app/static/js/csrf.js': ["evt.detail.headers['X-CSRFToken'] = token"],
    'app/templates/chat.html': [
        'id="global-search-form"',
        'ws-connect="/ws/chat"',
        'hx-ext="ws"',
        'data-notification-sound=',
        'data-send-on-enter=',
        'data-current-user-id=',
        'data-current-user-username=',
        'data-current-user-status=',
        'data-activity-url=',
        'id="connection-status-bar"',
        'id="chat-messages-container"',
        'id="jump-to-bottom-btn"',
        'class="jump-label"',
        'id="pane-progress"',
        'id="quick-switcher"',
        'id="quick-switcher-input"',
        'id="quick-switcher-results"',
        'partials/confirm_modal.html',
        'id="dm-self-item"',
        'hx-sync="#chat-messages-container:replace"',
        'data-current-user-display-name=',
        'id="channel-list"',
        'id="dm-list"',
        'id="unreads-link"',
        'id="link-{{ conv_id_str }}"',
        'id="unread-badge-{{ conv_id_str }}"',
        'id="dm-item-{{ conv_id_str }}"',
        'id="dm-self-item"',
        'id="status-dot-{{ g.user.id }}"',
        'id="status-dot-{{ user.id }}"',
        'class="incident-time"',
        'data-format="datetime"',
        'id="pager-overlay"',
        'id="pager-incidents"',
        'id="pager-sound-hint"',
        'id="right-panel-offcanvas"',
        'id="enable-notifications-banner"',
        'id="enable-notifications-btn"',
        'hx-get="/chat/input/default" hx-trigger="load"',
    ],
    // Admin pages (tests/js/admin.test.js).
    'app/templates/admin/layout.html': ['partials/confirm_modal.html', "static_url('js/admin.js')"],
    'app/templates/admin/dashboard_content.html': [
        'id="messagesChart"',
        "data-chart-src=\"{{ static_url('js/local/chart.umd.min.js') }}\"",
        '<script type="application/json" id="dashboard-chart-data">',
        '"hours": chart_hours',
        '"values": chart_values',
    ],
    'app/templates/admin/oncall_policy.html': [
        'level-target-type',
        'data-prefix="{{ prefix }}"',
        '{{ prefix }}-schedule',
        '{{ prefix }}-group',
        '{{ prefix }}-user',
        'data-confirm="Remove level {{ level.level }}?"',
    ],
    'app/templates/admin/oncall_schedule.html': [
        'use-shift-dates',
        'data-start=',
        'data-end=',
        'id="override-form-title"',
        'id="override-user"',
        'id="override-start"',
        'id="override-end"',
        'data-confirm="Remove this override?"',
    ],
    'app/templates/admin/edit_channel.html': ['data-autosubmit', 'data-confirm='],
    'app/templates/admin/user_list_content.html': ['hx-confirm="Deactivate'],
    'app/templates/partials/confirm_modal.html': [
        'id="confirm-modal"',
        'id="confirm-modal-body"',
        'id="confirm-modal-ok"',
        'data-bs-dismiss="modal"',
    ],
    'app/templates/partials/channel_messages.html': [
        'data-conversation-id="channel_{{ channel.id }}"',
        'data-conversation-db-id=',
        'id="message-list"',
        'id="message-list-bottom"',
        "partials/date_separator.html",
        'newer-message-loader',
    ],
    'app/templates/partials/dm_messages.html': [
        'data-conversation-id=',
        'id="message-list"',
        'id="message-list-bottom"',
        'newer-message-loader',
    ],
    'app/templates/partials/message.html': [
        'message-container',
        '{% if client_id %}data-client-id="{{ client_id }}"{% endif %}',
        'id="message-{{ message.id }}"',
        'data-user-id="{{ message.user.id }}"',
        'local-time',
        'data-timestamp="{{ message.created_at.isoformat() }}Z"',
        'id="reactions-container-{{ message.id }}"',
        'class="system-message"',
        'data-created-at="{{ message.created_at.isoformat() }}Z"',
    ],
    'app/templates/partials/message_toolbar.html': [
        'data-action="edit"',
        'data-bs-toggle="popover"',
        'data-copy-message',
        'class="copy-source"',
    ],
    'app/templates/partials/dm_user_results.html': ['data-focus-composer'],
    'app/templates/partials/chat_input_edit.html': [
        'hx-put="/chat/message/{{ message.id }}"',
        'data-after-save="/chat/input/default"',
        'data-after-save-target="#chat-input-container"',
        'data-editor=""',
        'data-send-label=',
    ],
    'app/templates/partials/chat_input_thread_edit.html': [
        'data-after-save="/chat/input/thread/{{ message.parent_message.id }}"',
        'data-after-save-target="#thread-input-container-{{ message.parent_message.id }}"',
        'data-editor="-thread-{{ message.parent_message.id }}"',
    ],
    'app/templates/partials/chat_input_thread.html': [
        'id="thread-input-container-{{ parent_message.id }}"',
        'id="message-form-thread-{{ parent_message.id }}"',
        'data-editor="-thread-{{ parent_message.id }}"',
        'name="parent_message_id"',
        'data-value="blockquote"',
    ],
    'app/templates/partials/chat_input_thread_reply.html': ['data-editor="-thread-{{ parent_message.id }}"'],
    'app/templates/partials/chat_input_reply.html': ['data-editor=""'],
    'app/templates/partials/create_poll_modal.html': [
        'id="create-poll-form"',
        'data-max-options=',
        'id="poll-conversation-id-input"',
        'id="poll-options-container"',
        'poll-option-row',
        'name="options[]"',
        'id="poll-option-template"',
        'data-poll-add-option',
        'remove-poll-option-btn',
    ],
    'app/templates/partials/reactions.html': ['reaction-pill', 'data-reactor-ids='],
    'app/templates/partials/date_separator.html': ['class="date-separator"'],
    'app/templates/partials/unread_badge.html': [
        'hx-swap-oob="innerHTML:#unread-badge-{{ conv_id_str }}"',
        'hx-swap-oob="outerHTML:#link-{{ conv_id_str }}"',
        'class="badge rounded-pill bg-danger"',
    ],
    'app/templates/partials/bold_link.html': ['hx-swap-oob="outerHTML:#link-{{ conv_id_str }}"', 'fw-bold'],
    'app/templates/partials/clear_badge.html': [
        '<span id="unread-badge-{{ conv_id_str }}" hx-swap-oob="outerHTML"></span>',
        'hx-swap-oob="outerHTML:#link-{{ conv_id_str }}"',
    ],
    'app/templates/partials/unreads_link_read.html': ['id="unreads-link"', 'hx-swap-oob="true"'],
    'app/templates/partials/unreads_link_unread.html': ['id="unreads-link"', 'hx-swap-oob="true"', 'fw-bold'],
    'app/templates/partials/dm_list_item_oob.html': [
        'hx-swap-oob="beforeend:#dm-list"',
        'id="dm-item-{{ conv_id_str }}"',
        'id="status-dot-{{ user.id }}"',
        'id="link-{{ conv_id_str }}"',
        'id="unread-badge-{{ conv_id_str }}"',
    ],
    'app/templates/partials/subscribe_oob.html': ['id="ws-subscriber-{{ conv_id_str }}"'],
    'app/templates/partials/_sidebar_profile_button.html': ['id="sidebar-presence-indicator-{{ g.user.id }}"'],
    'app/templates/partials/profile_header.html': [
        'id="profile-presence-indicator-{{ user.id }}"',
        'data-presence-choice="online"',
        'data-presence-choice="away"',
        'data-presence-choice="busy"',
        'data-autosubmit',
    ],
    'app/templates/partials/chat_input_default.html': [
        'id="chat-input-container"',
        'id="typing-indicator"',
        'hx-post="/chat/conversations/_pending_/messages"',
        'id="message-form"',
        'id="file-attachment-input"',
        'id="attachment-file-ids"',
        'id="attachment-previews"',
        'id="wysiwyg-editor"',
        'id="markdown-toggle-view"',
        'id="chat-message-input" name="chat_message"',
        'id="format-toggle-btn"',
        'id="file-attachment-btn"',
        'id="mention-btn"',
        'id="emoji-btn"',
        'id="send-button"',
        'id="emoji-picker-container"',
        '<emoji-picker',
        'id="typing-sender"',
        'data-editor=""',
    ],
    'app/templates/partials/mention_results.html': ['mention-suggestion-item', 'data-username="{{ user.username }}"'],
    'app/templates/partials/incident_card.html': ['id="incident-card-{{ incident.id }}"', 'incident-time'],
    'app/htmx_oob.py': [
        'f\'<div hx-swap-oob="{swap}:{safe_sel}">{inner_html}</div>\'',
        'f\'<div hx-swap-oob="beforeend:#message-list" \'',
        'f\'data-conversation-id="{safe_conv}">{inner_html}</div>\'',
    ],
    // Endpoints the scripts call by literal path.
    'app/routes.py': [
        '"/chat/conversations/<conv_id_str>/messages/since/<int:last_id>"',
        '"/chat/sidebar/unreads"',
        '"X-D8-Catchup"',
        // WS replies chat.js handles: the heartbeat and the subscribe ack.
        'if event_type == "ping":',
        '{"type": "pong"}',
        '{"type": "subscribed", "conversation_id": conv_id_str}',
    ],
    'app/blueprints/messages.py': [
        '"/chat/conversations/<conv_id_str>/messages", methods=["POST"]',
        '"/chat/message/<int:message_id>/context"',
    ],
    'app/blueprints/channels.py': ['"/chat/conversation/<conversation_id_str>/mention_search"'],
    'app/blueprints/oncall.py': [
        '"/oncall/pages/active"',
        '"/oncall/incidents/<int:incident_id>/ack", methods=["POST"]',
    ],
    'app/blueprints/profile.py': [
        '"/profile/activity", methods=["POST"]',
        // preferenceSelects(): the routes and the HX-Trigger events they emit.
        '"/profile/send_on_enter", methods=["PUT"]',
        'json.dumps({"update-send-on-enter": enabled})',
        '"/profile/notification_sound", methods=["PUT"]',
        '{"update-sound-preference": new_sound}',
    ],
    'app/templates/partials/profile_details.html': [
        'id="send-key-select"',
        'name="send_on_enter"',
        "hx-put=\"{{ url_for('profile.update_send_on_enter') }}\"",
        'id="notification-sound-select"',
        'data-preview-sound',
        'name="sound"',
        "hx-put=\"{{ url_for('profile.update_notification_sound') }}\"",
    ],
};

describe('fixture hooks still exist in the templates and routes', () => {
    for (const [file, hooks] of Object.entries(HOOKS)) {
        it(file, () => {
            const source = readSource(file);
            const missing = hooks.filter((hook) => !source.includes(hook));
            expect(missing, `update tests/js/helpers/fixtures.js to match ${file}`).toEqual([]);
        });
    }

    it('loads the scripts in the order the fixtures assume', () => {
        const base = readSource('app/templates/base.html');
        const order = ['js/local/htmx.min.js', 'js/local/htmx-ws.js', 'js/local/turndown.js', 'js/csrf.js', 'js/ui.js'].map((s) => base.indexOf(s));
        expect(order.every((i) => i > 0)).toBe(true);
        expect([...order].sort((a, b) => a - b)).toEqual(order);

        const chat = readSource('app/templates/chat.html');
        const deferred = ['emoji-picker-element', 'js/chat.js', 'js/pager.js'].map((s) => chat.indexOf(s));
        expect(deferred.every((i) => i > 0)).toBe(true);
        expect([...deferred].sort((a, b) => a - b)).toEqual(deferred);
    });
});
