/**
 * D8-Chat Main JavaScript File
 */

// ... (previewNotificationSound and NotificationManager objects remain the same) ...
function previewNotificationSound(soundFile) {
    if (soundFile) {
        const audio = new Audio(`/audio/${soundFile}`);
        audio.play().catch(error => { console.error("Sound preview failed:", error); });
    }
}

const NotificationManager = {
    soundFile: 'd8-notification.mp3',
    audio: null,
    initialize: function() {
        const mainContent = document.querySelector('main.main-content');
        if (mainContent && mainContent.dataset.notificationSound) {
            this.soundFile = mainContent.dataset.notificationSound;
        }
        if (!("Notification" in window)) {
            console.log("This browser does not support desktop notification");
            return;
        }
        const banner = document.getElementById('enable-notifications-banner');
        const button = document.getElementById('enable-notifications-btn');
        if (!banner || !button) return;
        if (Notification.permission === "default") {
            banner.style.display = 'block';
            button.addEventListener('click', this.requestPermission.bind(this));
        }
    },
    requestPermission: function() {
        Notification.requestPermission().then(permission => {
            const banner = document.getElementById('enable-notifications-banner');
            if (banner) banner.style.display = 'none';
            if (permission === "granted") {
                this.playSound('d8-notification.mp3');
            }
        });
    },
    playSound: function(soundFile) {
        if (!document.hasFocus()) {
            const fileToPlay = soundFile || this.soundFile;
            this.audio = new Audio(`/audio/${fileToPlay}`);
            this.audio.play().catch(error => { console.log("Audio play failed:", error); });
        }
    },
    showNotification: function(data) {
        // 1. If the app is focused, or OS notifications aren't allowed/supported (e.g., local HTTP),
        // fallback to our in-app ToastManager so you still see the message!
        if (document.hasFocus() || !("Notification" in window) || Notification.permission !== "granted") {
            if (typeof ToastManager !== 'undefined') {
                // Use 'primary' color for standard info instead of the default red 'danger'
                ToastManager.show(data.title, data.body, 'primary', true);
            }
            return;
        }

        // 2. Otherwise, show the native OS system notification
        if (Notification.permission === "granted") {
            const notification = new Notification(data.title, {
                body: data.body,
                icon: data.icon,
                tag: data.tag
            });

            // Auto-close after 5 seconds so it pops up and then disappears
            setTimeout(() => notification.close(), 5000);

            notification.onclick = () => {
                window.focus();
                notification.close();
                // The tag is the conversation id: open it.
                const link = data.tag && document.getElementById(`link-${data.tag}`);
                const anchor = link && link.closest('a[hx-get]');
                if (anchor) htmx.trigger(anchor, 'click');
            };
        }
    }
};

const ToastManager = {
    toastEl: null,
    headerEl: null,
    titleEl: null,
    bodyEl: null,
    bootstrapToast: null,
    initialize: function() {
        this.toastEl = document.getElementById('app-toast');
        this.headerEl = document.getElementById('toast-header');
        this.titleEl = document.getElementById('toast-title');
        this.bodyEl = document.getElementById('toast-body-content');
        if (this.toastEl) this.bootstrapToast = new bootstrap.Toast(this.toastEl, { delay: 5000 });
    },
    show: function(title, message, level = 'danger', autohide = true) {
        if (!this.bootstrapToast || !this.titleEl || !this.bodyEl) return;
        this.titleEl.textContent = title;
        this.bodyEl.textContent = message;
        this.headerEl.className = 'toast-header';
        this.headerEl.classList.add(`bg-${level}`, 'text-white');
        this.bootstrapToast = new bootstrap.Toast(this.toastEl, { autohide: autohide, delay: 5000 });
        this.bootstrapToast.show();
    },
    hide: function() {
        if (this.bootstrapToast) { this.bootstrapToast.hide(); }
    }
};


// A short, human message for a failed request: the server's own text when it
// sent a short plain one, a JSON body's "error", otherwise one by status. Raw
// bodies (an HTML error page, a stack of JSON) used to land in the toast.
function friendlyErrorText(xhr) {
    if (!xhr) return 'Please try again.';
    const body = (xhr.responseText || '').trim();
    const type = (xhr.getResponseHeader && xhr.getResponseHeader('Content-Type')) || '';
    if (type.includes('application/json')) {
        try {
            const data = JSON.parse(body);
            if (data && typeof data.error === 'string') return data.error;
        } catch (e) { /* fall through to the status */ }
    } else if (body && body.length <= 160 && !body.startsWith('<')) {
        return body;
    }
    if (xhr.status === 403) return "You don't have access to that.";
    if (xhr.status === 404) return 'That no longer exists.';
    if (xhr.status === 413) return 'That file is too large.';
    if (xhr.status === 429) return 'Too many requests. Wait a moment and try again.';
    if (xhr.status >= 500) return 'The server had a problem. Please try again.';
    return 'Please try again.';
}

/**
 * Date dividers in the viewer's own timezone. The server buckets messages by
 * UTC day (and labels a broadcast as of the sender's clock), so for anyone
 * west of UTC an evening message sat under the next day's divider. Every
 * message carries its UTC timestamp (data-created-at); this rebuilds the
 * dividers from local calendar days whenever the list changes, which also
 * merges the duplicate divider left where an older page meets the one below.
 */
const DateDividers = {
    label(date, now = new Date()) {
        const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
        const days = Math.round((startOf(now) - startOf(date)) / 86400000);
        if (days === 0) return 'Today';
        if (days === 1) return 'Yesterday';
        const n = date.getDate();
        const suffix = (n % 10 === 1 && n !== 11) ? 'st'
            : (n % 10 === 2 && n !== 12) ? 'nd'
            : (n % 10 === 3 && n !== 13) ? 'rd' : 'th';
        const month = date.toLocaleString('en-US', { month: 'long' });
        return `${month} ${n}${suffix}${date.getFullYear() === now.getFullYear() ? '' : `, ${date.getFullYear()}`}`;
    },
    make(text) {
        const el = document.createElement('div');
        el.className = 'date-separator';
        el.setAttribute('role', 'separator');
        const span = document.createElement('span');
        span.textContent = text;
        el.appendChild(span);
        return el;
    },
    rebuild(list) {
        if (!list) return;
        const items = [...list.children].filter((el) => el.matches('.message-container, .system-message'));
        // Nothing stamped (an empty pane, older markup): keep the server's.
        if (!items.some((el) => el.dataset.createdAt)) return;
        const wanted = [];
        let previousDay = null;
        items.forEach((el) => {
            if (!el.dataset.createdAt) return; // an optimistic copy: same day as its neighbours
            const when = new Date(el.dataset.createdAt);
            if (Number.isNaN(when.getTime())) return;
            const day = when.toDateString();
            if (day !== previousDay) wanted.push({ el, text: this.label(when) });
            previousDay = day;
        });
        list.querySelectorAll(':scope > .date-separator').forEach((el) => el.remove());
        wanted.forEach(({ el, text }) => {
            // Above the unread line that may sit right on top of the message.
            let anchor = el;
            while (anchor.previousElementSibling && anchor.previousElementSibling.matches('.new-message-separator')) {
                anchor = anchor.previousElementSibling;
            }
            anchor.before(this.make(text));
        });
    },
};
window.DateDividers = DateDividers;

// A sending tab's id for its optimistic copy of a message; the server echoes
// it on the rendered message so the copy can be swapped for the real one.
function newClientId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

const activeConversationId = () => {
    const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
    return activeConv ? activeConv.dataset.conversationId : null;
};

// The name the sidebar shows for a conversation ("# general", "Robert").
const conversationName = (convId) => {
    const link = convId && document.getElementById(`link-${convId}`);
    return link ? link.textContent.replace(/\s+/g, ' ').replace(/\(you\)$/, '').trim() : '';
};

/**
 * Unsent composer text per conversation, so switching away and back (or
 * reloading) doesn't lose what you were writing. Kept in memory and mirrored to
 * localStorage, which can be unavailable (private windows), so every access is
 * guarded. The sidebar marks conversations that hold a draft.
 */
const DraftManager = {
    drafts: new Map(),
    storageKey: (convId) => `d8-draft:${convId}`,
    get(convId) {
        if (!convId) return '';
        if (this.drafts.has(convId)) return this.drafts.get(convId);
        try {
            return window.localStorage.getItem(this.storageKey(convId)) || '';
        } catch (e) {
            return '';
        }
    },
    save(convId, text) {
        if (!convId) return;
        const value = (text || '').trim() ? text : '';
        this.drafts.set(convId, value);
        try {
            if (value) window.localStorage.setItem(this.storageKey(convId), value);
            else window.localStorage.removeItem(this.storageKey(convId));
        } catch (e) { /* storage unavailable: the in-memory copy still works */ }
        this.mark(convId, !!value);
    },
    clear(convId) {
        this.save(convId, '');
    },
    mark(convId, hasDraft) {
        const link = document.getElementById(`link-${convId}`);
        if (link) link.classList.toggle('has-draft', hasDraft);
    },
    markAll() {
        document.querySelectorAll('[id^="link-"]').forEach((link) => {
            const convId = link.id.slice('link-'.length);
            link.classList.toggle('has-draft', !!this.get(convId));
        });
    },
};

/** "(3) # general · D8 Chat": the open conversation plus the unread count. */
const TitleManager = {
    base: 'D8 Chat',
    current() {
        const count = FaviconManager.calculateUnreadCount();
        const name = conversationName(activeConversationId());
        return `${count > 0 ? `(${count}) ` : ''}${name ? `${name} · ` : ''}${this.base}`;
    },
    update() {
        // The pager flashes the title while paging; it restores current() when done.
        if (window.PagerManager && window.PagerManager.titleTimer) return;
        const title = this.current();
        if (document.title !== title) document.title = title;
    },
};
window.TitleManager = TitleManager;

/** "Message # general" in the empty main composer. */
const ComposerPlaceholder = {
    update() {
        const name = conversationName(activeConversationId());
        const text = name ? `Message ${name}` : 'Message';
        const editor = document.getElementById('wysiwyg-editor');
        const markdown = document.getElementById('markdown-toggle-view');
        if (editor) {
            editor.dataset.placeholder = text;
            editor.setAttribute('aria-label', text);
        }
        if (markdown) {
            markdown.placeholder = text;
            markdown.setAttribute('aria-label', text);
        }
    },
};

/**
 * Optimistic sends. A message shows up the moment it's sent, dimmed, and is
 * swapped for the server's rendering when that arrives (in the POST response
 * or over the WebSocket, whichever is first, matched by client_id). A send
 * that fails stays put with Retry / Edit / Discard instead of vanishing.
 */
const PendingMessages = {
    sends: new Map(),
    add({ clientId, text, attachments, listSelector, path, values }) {
        const list = document.querySelector(listSelector);
        this.sends.set(clientId, { path, values, text, listSelector });
        if (!list) return;
        const main = document.querySelector('main.main-content');
        const me = main ? main.dataset.currentUserId : null;
        const visible = list.querySelectorAll('.message-container[data-user-id], .message-container.pending-message');
        const last = visible[visible.length - 1];
        const grouped = !!last && (last.classList.contains('pending-message') || last.dataset.userId === me);

        const el = document.createElement('div');
        el.className = `d-flex mb-1 message-container pending-message${grouped ? ' grouped-message' : ''}`;
        el.id = `pending-${clientId}`;
        el.dataset.clientId = clientId;
        el.innerHTML = `
            <div class="flex-shrink-0 pending-avatar"></div>
            <div class="flex-grow-1 ms-3">
                <div class="pending-header"><strong class="me-2"></strong></div>
                <div class="message-content pending-text"></div>
                <div class="pending-attachments small text-muted"></div>
                <div class="pending-status small text-muted" role="status">Sending…</div>
            </div>`;
        if (grouped) {
            el.querySelector('.pending-header').remove();
        } else {
            el.querySelector('.pending-header strong').textContent = main ? (main.dataset.currentUserDisplayName || main.dataset.currentUserUsername || '') : '';
            const avatar = document.querySelector('#sidebar-profile-button .avatar-image');
            if (avatar) {
                const copy = avatar.cloneNode(true);
                copy.removeAttribute('id');
                copy.style.width = '40px';
                copy.style.height = '40px';
                el.querySelector('.pending-avatar').appendChild(copy);
            }
        }
        el.querySelector('.pending-text').textContent = text;
        if (attachments > 0) {
            el.querySelector('.pending-attachments').textContent = `📎 ${attachments} ${attachments === 1 ? 'attachment' : 'attachments'}`;
        }
        list.appendChild(el);
        this.scrollIntoView(list);
    },
    scrollIntoView(list) {
        const scroller = list.closest('#chat-messages-container')
            || list.closest('.flex-grow-1[style*="overflow-y: auto"]');
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
    },
    // The real message arrived: drop its stand-in.
    resolve(clientId) {
        const el = document.getElementById(`pending-${clientId}`);
        if (el) el.remove();
        this.sends.delete(clientId);
    },
    fail(clientId, reason) {
        const el = document.getElementById(`pending-${clientId}`);
        if (!el) return;
        el.classList.add('failed');
        const status = el.querySelector('.pending-status');
        status.textContent = '';
        const label = document.createElement('span');
        label.className = 'text-danger me-2';
        label.textContent = reason || 'Not sent.';
        status.appendChild(label);
        [['retry', 'Retry'], ['edit', 'Edit'], ['discard', 'Discard']].forEach(([action, text]) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-link btn-sm p-0 me-2 pending-action';
            btn.dataset.pendingAction = action;
            btn.textContent = text;
            status.appendChild(btn);
        });
    },
    retry(clientId) {
        const send = this.sends.get(clientId);
        const el = document.getElementById(`pending-${clientId}`);
        if (!send || !el) return;
        el.classList.remove('failed');
        el.querySelector('.pending-status').textContent = 'Sending…';
        htmx.ajax('POST', send.path, { values: send.values, source: document.body, swap: 'none' }).catch(() => {});
    },
    discard(clientId) {
        this.resolve(clientId);
    },
    // Put the text back in the composer (if it's empty) and drop the copy.
    edit(clientId) {
        const send = this.sends.get(clientId);
        if (send && window.mainEditor && !window.mainEditor.getText().trim()) {
            window.mainEditor.setText(send.text);
        }
        this.resolve(clientId);
        if (window.mainEditor) window.mainEditor.focusActiveInput();
    },
    // Keep unconfirmed messages below everything that has arrived since.
    keepAtBottom(list) {
        list.querySelectorAll(':scope > .pending-message').forEach((el) => list.appendChild(el));
    },
};
window.PendingMessages = PendingMessages;

const FaviconManager = {
    faviconLink: null,
    originalFaviconHref: null,
    originalImage: null, // We'll store the loaded favicon image here
    isInitialized: false,
    currentUnreadCount: 0,

    initialize: function() {
        this.faviconLink = document.querySelector("link[rel='icon']");
        if (!this.faviconLink) {
            console.error("Favicon link tag not found.");
            return;
        }
        this.originalFaviconHref = new URL(this.faviconLink.href).href;

        // Pre-load the original favicon image so we can re-use it for drawing
        const img = document.createElement('img');
        img.src = this.originalFaviconHref;
        img.crossOrigin = "anonymous";
        img.onload = () => {
            this.originalImage = img;
            this.isInitialized = true;
            // Run an initial check once the image is ready
            this.updateFavicon();
        };
        img.onerror = () => {
            console.error("Failed to pre-load original favicon.");
        };
    },

    calculateUnreadCount: function() {
        let total = 0;
        // This selector finds all the visible red badges in the sidebar
        const badges = document.querySelectorAll('#channel-list .badge, #dm-list .badge');
        badges.forEach(badge => {
            const count = parseInt(badge.textContent, 10);
            if (!isNaN(count)) {
                total += count;
            }
        });
        return total;
    },

    drawFavicon: function(count) {
        if (!this.originalImage) return; // Can't draw if the base image isn't loaded

        const canvas = document.createElement('canvas');
        const size = 32;
        canvas.width = size;
        canvas.height = size;
        const context = canvas.getContext('2d');

        // 1. Draw the original favicon
        context.drawImage(this.originalImage, 0, 0, size, size);

        if (count > 0) {
            // 2. Draw a larger red dot without a border
            const dotRadius = size * 0.4; // Increased size
            const dotX = size - dotRadius;      // Simplified position
            const dotY = dotRadius;

            context.beginPath();
            context.arc(dotX, dotY, dotRadius, 0, 2 * Math.PI, false);
            context.fillStyle = '#d92626'; // Strong red
            context.fill();

            // 3. Draw the count text with a larger font
            const text = count > 9 ? '9+' : count.toString();
            context.fillStyle = '#ffffff'; // White text
            context.textAlign = 'center';
            context.textBaseline = 'middle';
            // Increased font sizes
            const fontSize = text.length > 1 ? size * 0.5 : size * 0.6;
            context.font = `bold ${fontSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`;
            // Nudge the text slightly for better centering within the circle
            context.fillText(text, dotX, dotY + 1);
        }

        // 4. Update the favicon link with the new canvas image
        this.faviconLink.href = canvas.toDataURL('image/png');
    },

    updateFavicon: function() {
        TitleManager.update();
        if (!this.isInitialized) return;

        // Instead of a boolean, we now get the actual count
        const newCount = this.calculateUnreadCount();

        // Only redraw the favicon if the count has changed
        if (newCount !== this.currentUnreadCount) {
            this.currentUnreadCount = newCount;
            if (newCount > 0) {
                this.drawFavicon(newCount);
            } else {
                // If there are no unreads, revert to the original
                this.faviconLink.href = this.originalFaviconHref;
            }
        }
    }
};


/**
 * Factory function for the Attachment Manager.
 */
const createAttachmentManager = function(editorState) {
    const state = {
        fileInput: document.getElementById(`file-attachment-input${editorState.idSuffix}`),
        attachmentBtn: document.getElementById(`file-attachment-btn${editorState.idSuffix}`),
        hiddenAttachmentIds: document.getElementById(`attachment-file-ids${editorState.idSuffix}`),
        previewContainer: document.getElementById(`attachment-previews${editorState.idSuffix}`),
        uploads: new Map()
    };

    const initialize = function() {
        if (!state.fileInput || !state.attachmentBtn || !state.previewContainer || !state.hiddenAttachmentIds) {
            return;
        }
        state.attachmentBtn.addEventListener('click', () => state.fileInput.click());
        state.fileInput.addEventListener('change', handleFileSelect);
        state.previewContainer.addEventListener('click', (e) => {
            if (e.target.classList.contains('remove-attachment-btn')) {
                const thumbnail = e.target.closest('.attachment-thumbnail');
                if (thumbnail) removeAttachment(thumbnail.dataset.uploadKey);
            }
        });
        if (editorState.editor) editorState.editor.addEventListener('paste', handlePaste);
        if (editorState.markdownView) editorState.markdownView.addEventListener('paste', handlePaste);
    };

    const handlePaste = function(e) {
        const items = (e.clipboardData || e.originalEvent.clipboardData).items;
        let foundImage = false;
        const filesToUpload = [];
        for (const item of items) {
            if (item.kind === 'file' && item.type.startsWith('image/')) {
                foundImage = true;
                filesToUpload.push(item.getAsFile());
            }
        }
        if (foundImage) {
            e.preventDefault();
            processAndUploadFiles(filesToUpload);
        }
    };

    const handleFileSelect = function(e) {
        const files = e.target.files;
        if (!files.length) return;
        processAndUploadFiles(files);
        state.fileInput.value = '';
    };

    const processAndUploadFiles = function(files) {
        if ((state.uploads.size + files.length) > 30) {
            ToastManager.show('Upload Limit', 'You can only attach up to 30 files per message.', 'warning');
            return;
        }
        for (const file of files) {
            const uploadKey = `upload-${Date.now()}-${Math.random()}`;
            state.uploads.set(uploadKey, { file: file, fileId: null, status: 'pending' });
            createPreviewAndUpload(file, uploadKey);
        }
    };

    const createPreviewAndUpload = function(file, uploadKey) {
        state.previewContainer.classList.add('has-attachments');

        const thumbnailDiv = document.createElement('div');
        thumbnailDiv.className = 'attachment-thumbnail';
        thumbnailDiv.dataset.uploadKey = uploadKey;

        const isImage = file.type.startsWith('image/');

        let iconPath = '';
        if (file.type === 'application/pdf') {
            iconPath = '/icons/pdf-icon.svg';
        } else if (!isImage) {
            iconPath = '/icons/generic-file-icon.svg';
        }

        thumbnailDiv.innerHTML = `
            ${isImage
                ? `<img src="" alt="Uploading..." />`
                : `<img src="${iconPath}" class="file-icon-img" alt="file" />`
            }
            <div class="file-name-small"></div>
            <div class="spinner-border spinner-border-sm text-light position-absolute top-50 start-50"></div>
            <button type="button" class="remove-attachment-btn">&times;</button>
        `;
        thumbnailDiv.querySelector('.file-name-small').textContent = file.name.substring(0, 15);


        state.previewContainer.appendChild(thumbnailDiv);

        if (isImage) {
            const reader = new FileReader();
            reader.onload = (e) => {
                thumbnailDiv.querySelector('img').src = e.target.result;
            };
            reader.readAsDataURL(file);
        }

        // 3. Upload logic remains the same
        const formData = new FormData();
        formData.append('file', file);

        // CSRF token — this fetch bypasses HTMX so the global
        // `htmx:configRequest` hook doesn't run. Read directly from the meta
        // tag we set in base.html and attach manually.
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
        const headers = csrfToken ? { 'X-CSRFToken': csrfToken } : {};

        fetch('/files/upload', { method: 'POST', body: formData, headers })
            .then(response => {
                const spinner = thumbnailDiv.querySelector('.spinner-border');
                if (spinner) spinner.remove();

                if (!response.ok) return response.json().then(err => { throw err; });
                return response.json();
            })
            .then(data => {
                if (data && data.file_id) {
                    const upload = state.uploads.get(uploadKey);
                    if (upload) {
                        upload.fileId = data.file_id;
                        upload.status = 'success';
                    }
                    updateHiddenInput();
                }
            })
            .catch(error => {
                const errorMessage = error.error || "Upload failed";
                console.error("Upload failed for key", uploadKey, ":", errorMessage);
                if (typeof ToastManager !== 'undefined') {
                    ToastManager.show('Upload Error', errorMessage, 'danger');
                }

                const upload = state.uploads.get(uploadKey);
                if (upload) upload.status = 'error';
                thumbnailDiv.style.opacity = '0.5';
                thumbnailDiv.title = errorMessage;
                setTimeout(() => {
                    removeAttachment(uploadKey)
                }, 3000);

            });
    };

    const removeAttachment = function(uploadKey) {
        const thumbnail = state.previewContainer.querySelector(`[data-upload-key="${uploadKey}"]`);
        if (thumbnail) thumbnail.remove();
        state.uploads.delete(uploadKey);
        updateHiddenInput();
        if (state.uploads.size === 0) {
            state.previewContainer.classList.remove('has-attachments');
        }
    };

    const updateHiddenInput = function() {
        const successfulIds = Array.from(state.uploads.values())
            .filter(u => u.status === 'success' && u.fileId)
            .map(u => u.fileId);
        state.hiddenAttachmentIds.value = successfulIds.join(',');
    };

    const reset = function() {
        if (!state.previewContainer || !state.hiddenAttachmentIds) return;
        state.previewContainer.innerHTML = '';
        state.previewContainer.classList.remove('has-attachments');
        state.uploads.clear();
        updateHiddenInput();
    };

    // [THE FIX] Expose the processAndUploadFiles function so we can call it from outside.
    return {
        initialize,
        reset,
        processAndUploadFiles
    };
};

// ... (createMentionManager object remains the same) ...
const createMentionManager = function(editorState) {
    const state = {
        editorState, popoverContainer: null, active: false, triggerPosition: -1, query: '', isSelecting: false
    };
    const initialize = function() {
        const popoverContainer = document.createElement('div');
        popoverContainer.className = 'border rounded shadow-sm bg-body';
        popoverContainer.style.cssText = 'position: absolute; bottom: 100%; left: 40px; z-index: 1060; display: none; max-height: 250px; width: 300px; overflow-y: auto;';
        state.editorState.messageForm.appendChild(popoverContainer);
        state.popoverContainer = popoverContainer;
        bindEvents();
    };
    const bindEvents = function() {
        const { editor, markdownView } = state.editorState;
        editor.addEventListener('input', handleInput);
        markdownView.addEventListener('input', handleInput);
        editor.addEventListener('keydown', handleKeyDown);
        markdownView.addEventListener('keydown', handleKeyDown);
        state.popoverContainer.addEventListener('click', (e) => {
            const item = e.target.closest('.mention-suggestion-item');
            if (!item) return;
            // selectItem empties the popover, detaching the clicked item while
            // the click is still bubbling. htmx's submitter tracking on the
            // enclosing form would then throw on the detached button.
            e.stopPropagation();
            selectItem(item);
        });
    };
    const handleInput = function() {
        if (state.isSelecting) { state.isSelecting = false; return; }
        const { isMarkdownMode, markdownView, editor } = state.editorState;
        let text, cursorPosition;
        if (isMarkdownMode) {
            text = markdownView.value;
            cursorPosition = markdownView.selectionStart;
        } else {
            const selection = window.getSelection();
            if (selection.rangeCount === 0) return;
            const range = selection.getRangeAt(0);
            // Range offsets are local to the caret's text node, so the trigger
            // search must run on that node's text — not the whole editor's —
            // or the two disagree as soon as the editor holds multiple nodes.
            const node = range.startContainer;
            text = node.nodeType === Node.TEXT_NODE ? node.textContent : '';
            cursorPosition = range.startOffset;
        }
        const textUpToCursor = text.substring(0, cursorPosition);
        const triggerIndex = textUpToCursor.lastIndexOf('@');
        if (triggerIndex !== -1 && (triggerIndex === 0 || /\s/.test(textUpToCursor[triggerIndex - 1]))) {
            state.query = textUpToCursor.substring(triggerIndex + 1);
            state.triggerPosition = triggerIndex;
            if (state.query.includes(' ')) { hidePopover(); return; }
            fetchUsers(state.query);
        } else {
            hidePopover();
        }
    };
    const handleKeyDown = function(e) {
        if (!state.active) return;
        const items = state.popoverContainer.querySelectorAll('.mention-suggestion-item');
        if (items.length === 0) return;
        let activeItem = state.popoverContainer.querySelector('.mention-suggestion-item.active');
        if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) {
            e.preventDefault();
            // Immediate: the editor's own keydown handler (same element) would
            // otherwise see Enter with the popover closed and send the message.
            e.stopImmediatePropagation();
            if (e.key === 'ArrowDown') {
                const next = activeItem.nextElementSibling || items[0];
                activeItem.classList.remove('active');
                next.classList.add('active');
            } else if (e.key === 'ArrowUp') {
                const prev = activeItem.previousElementSibling || items[items.length - 1];
                activeItem.classList.remove('active');
                prev.classList.add('active');
            } else if (e.key === 'Enter' || e.key === 'Tab') {
                selectItem(activeItem);
            } else if (e.key === 'Escape') {
                hidePopover();
            }
        }
    };
    const fetchUsers = function(query) {
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        if (!activeConv) { hidePopover(); return; }
        const conversationIdStr = activeConv.dataset.conversationId;
        const url = `/chat/conversation/${conversationIdStr}/mention_search?q=${encodeURIComponent(query)}`;
        htmx.ajax('GET', url, { target: state.popoverContainer, swap: 'innerHTML' });
        showPopover();
    };
    const selectItem = function(item) {
        state.isSelecting = true;
        const username = item.dataset.username;
        const { isMarkdownMode, markdownView, editor } = state.editorState;
        if (isMarkdownMode) {
            const text = markdownView.value;
            const pre = text.substring(0, state.triggerPosition);
            const post = text.substring(markdownView.selectionStart);
            markdownView.value = `${pre}@${username} ${post}`;
            const newCursorPos = (pre + `@${username} `).length;
            markdownView.focus();
            markdownView.setSelectionRange(newCursorPos, newCursorPos);
        } else {
            editor.focus();
            const selection = window.getSelection();
            const range = selection.getRangeAt(0);
            const textNode = range.startContainer;
            // The caret offset must be captured before setStart mutates the
            // range: setEnd(textNode, range.startOffset) after setStart made a
            // collapsed range, so the typed "@quer" was never deleted and the
            // mention landed next to it ("@josue_sahad @jose").
            const caretOffset = range.startOffset;
            if (textNode.nodeType === Node.TEXT_NODE && state.triggerPosition <= caretOffset) {
                range.setStart(textNode, state.triggerPosition);
                range.setEnd(textNode, caretOffset);
            }
            range.deleteContents();
            const pill = document.createElement('span');
            pill.className = 'mention-token';
            pill.setAttribute('contenteditable', 'false');
            pill.textContent = `@${username}`;
            range.insertNode(pill);
            // NBSP rather than ' ': a plain trailing space collapses at the
            // end of a contenteditable line and the caret sticks to the pill.
            // Serialization normalizes it back to a regular space.
            const spacer = document.createTextNode('\u00A0');
            pill.after(spacer);
            range.setStart(spacer, 1);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
        }
        hidePopover();
        (isMarkdownMode ? markdownView : editor).dispatchEvent(new Event('input', { bubbles: true }));
    };
    const showPopover = function() {
        state.popoverContainer.style.display = 'block';
        state.active = true;
    };
    const hidePopover = function() {
        state.popoverContainer.style.display = 'none';
        state.popoverContainer.innerHTML = '';
        state.active = false;
    };
    return { initialize, state };
};

// ---------------------------------------------------------------------------
// Emoji autocomplete + inline ":shortcode:" conversion.
//
// Reuses the SAME local emoji dataset the <emoji-picker> element loads, so
// shortcodes stay consistent everywhere in the app. The index is fetched once
// and shared across every editor instance.
// ---------------------------------------------------------------------------
const EMOJI_DATA_SOURCE = '/js/local/emoji.json';
// Characters that may appear in an emoji shortcode (letters, digits, _ + -).
const EMOJI_SHORTCODE_CHARS = 'a-zA-Z0-9_+\\-';
let emojiIndexPromise = null;
const getEmojiIndex = function() {
    if (!emojiIndexPromise) {
        emojiIndexPromise = fetch(EMOJI_DATA_SOURCE)
            .then(r => r.json())
            .then(data => {
                const list = [];              // {shortcode, emoji, order} for search
                const byShortcode = new Map(); // exact shortcode -> emoji, for conversion
                data.forEach(entry => {
                    if (!entry.emoji || !entry.shortcodes) return;
                    entry.shortcodes.forEach(sc => {
                        const key = sc.toLowerCase();
                        list.push({ shortcode: key, emoji: entry.emoji, order: entry.order || 0 });
                        if (!byShortcode.has(key)) byShortcode.set(key, entry.emoji);
                    });
                });
                return { list, byShortcode };
            })
            .catch(() => ({ list: [], byShortcode: new Map() }));
    }
    return emojiIndexPromise;
};

const createEmojiManager = function(editorState) {
    const MIN_QUERY = 2;   // don't pop up until the user has typed a couple of letters
    const MAX_RESULTS = 8; // keep the list short, like WhatsApp's strip
    const state = {
        editorState,
        popoverContainer: null,
        active: false,
        query: '',
        index: null,
        isSelecting: false
    };

    const initialize = function() {
        const popoverContainer = document.createElement('div');
        popoverContainer.className = 'list-group border rounded shadow-sm bg-body emoji-suggestion-popover';
        popoverContainer.style.cssText = 'position: absolute; bottom: 100%; left: 40px; z-index: 1060; display: none; max-height: 250px; width: 260px; overflow-y: auto;';
        state.editorState.messageForm.appendChild(popoverContainer);
        state.popoverContainer = popoverContainer;
        // Lets the global outside-click handler dismiss us and reset our state.
        popoverContainer.emojiHide = hidePopover;
        getEmojiIndex().then(idx => { state.index = idx; });
        bindEvents();
    };

    const bindEvents = function() {
        const { editor, markdownView } = state.editorState;
        editor.addEventListener('input', handleInput);
        markdownView.addEventListener('input', handleInput);
        editor.addEventListener('keydown', handleKeyDown);
        markdownView.addEventListener('keydown', handleKeyDown);
        // mousedown (not click) so the editor keeps its selection/caret while we insert.
        state.popoverContainer.addEventListener('mousedown', (e) => {
            const item = e.target.closest('.emoji-suggestion-item');
            if (item) {
                e.preventDefault();
                selectEmoji(item.dataset.emoji);
            }
        });
    };

    // Returns the text preceding the caret plus the info needed to replace a
    // slice of it, for whichever input mode is active. null if unavailable.
    const getContext = function() {
        const { isMarkdownMode, markdownView } = state.editorState;
        if (isMarkdownMode) {
            const cursor = markdownView.selectionStart;
            return { textBeforeCursor: markdownView.value.substring(0, cursor), node: null, cursor };
        }
        const selection = window.getSelection();
        if (!selection.rangeCount) return null;
        const range = selection.getRangeAt(0);
        if (!range.collapsed) return null;
        const node = range.startContainer;
        if (node.nodeType !== Node.TEXT_NODE) return null;
        const cursor = range.startOffset;
        return { textBeforeCursor: node.textContent.substring(0, cursor), node, cursor };
    };

    const handleInput = function() {
        if (state.isSelecting) { state.isSelecting = false; return; }
        const ctx = getContext();
        if (!ctx) { hidePopover(); return; }
        // A colon at the start of a word, followed by shortcode characters, with
        // no closing colon yet.
        const m = ctx.textBeforeCursor.match(new RegExp('(?:^|\\s):([' + EMOJI_SHORTCODE_CHARS + ']+)$'));
        if (!m || m[1].length < MIN_QUERY) { hidePopover(); return; }
        state.query = m[1];
        showMatches(m[1]);
    };

    const showMatches = function(query) {
        if (!state.index) { hidePopover(); return; }
        const q = query.toLowerCase();
        const scored = [];
        for (const item of state.index.list) {
            const pos = item.shortcode.indexOf(q);
            if (pos === -1) continue;
            scored.push({ item, rank: pos === 0 ? 0 : 1 });
        }
        // Prefix matches first, then by the dataset's popularity ordering.
        scored.sort((a, b) => a.rank - b.rank || a.item.order - b.item.order);
        const seen = new Set();
        const matches = [];
        for (const s of scored) {
            if (seen.has(s.item.emoji)) continue; // one row per emoji
            seen.add(s.item.emoji);
            matches.push(s.item);
            if (matches.length >= MAX_RESULTS) break;
        }
        if (matches.length === 0) { hidePopover(); return; }
        renderPopover(matches);
        showPopover();
    };

    const renderPopover = function(matches) {
        state.popoverContainer.innerHTML = matches.map((mt, i) => (
            '<button type="button" class="list-group-item list-group-item-action emoji-suggestion-item d-flex align-items-center gap-2 p-2' + (i === 0 ? ' active' : '') + '"' +
            ' data-emoji="' + mt.emoji + '" data-shortcode="' + mt.shortcode + '">' +
            '<span class="emoji-suggestion-glyph">' + mt.emoji + '</span>' +
            '<span class="text-muted small">:' + mt.shortcode + ':</span>' +
            '</button>'
        )).join('');
    };

    const handleKeyDown = function(e) {
        // Inline conversion: ":shortcode:" + space -> emoji, popover open or not.
        if (e.key === ' ' || e.key === 'Spacebar') {
            if (tryConvertShortcodeBeforeCursor()) {
                e.preventDefault();
                e.stopImmediatePropagation();
            }
            return;
        }
        if (!state.active) return;
        const items = state.popoverContainer.querySelectorAll('.emoji-suggestion-item');
        if (items.length === 0) return;
        if (!['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) return;
        // stopImmediatePropagation so the editor's own keydown handler (same
        // element) doesn't also submit/insert a newline.
        e.preventDefault();
        e.stopImmediatePropagation();
        const activeItem = state.popoverContainer.querySelector('.emoji-suggestion-item.active') || items[0];
        if (e.key === 'ArrowDown') {
            const next = activeItem.nextElementSibling || items[0];
            activeItem.classList.remove('active');
            next.classList.add('active');
            next.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
            const prev = activeItem.previousElementSibling || items[items.length - 1];
            activeItem.classList.remove('active');
            prev.classList.add('active');
            prev.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            selectEmoji(activeItem.dataset.emoji);
        } else if (e.key === 'Escape') {
            hidePopover();
        }
    };

    // If the text right before the caret is a complete ":shortcode:", replace
    // it with the emoji (followed by the space the user just pressed).
    const tryConvertShortcodeBeforeCursor = function() {
        if (!state.index) return false;
        const ctx = getContext();
        if (!ctx) return false;
        const m = ctx.textBeforeCursor.match(new RegExp(':([' + EMOJI_SHORTCODE_CHARS + ']+):$'));
        if (!m) return false;
        const emoji = state.index.byShortcode.get(m[1].toLowerCase());
        if (!emoji) return false;
        replaceRange(ctx, ctx.cursor - m[0].length, ctx.cursor, emoji + ' ');
        hidePopover();
        return true;
    };

    const selectEmoji = function(emoji) {
        if (!emoji) { hidePopover(); return; }
        const ctx = getContext();
        if (!ctx) { hidePopover(); return; }
        // Replace the ":query" the caret sits after with the chosen emoji.
        replaceRange(ctx, ctx.cursor - state.query.length - 1, ctx.cursor, emoji);
        hidePopover();
    };

    const replaceRange = function(ctx, start, end, text) {
        const { isMarkdownMode, markdownView, editor } = state.editorState;
        state.isSelecting = true; // suppress our own re-trigger on the synthetic input event
        if (isMarkdownMode) {
            const val = markdownView.value;
            markdownView.value = val.substring(0, start) + text + val.substring(end);
            const pos = start + text.length;
            markdownView.focus();
            markdownView.setSelectionRange(pos, pos);
        } else {
            const range = document.createRange();
            range.setStart(ctx.node, start);
            range.setEnd(ctx.node, end);
            const sel = window.getSelection();
            editor.focus();
            sel.removeAllRanges();
            sel.addRange(range);
            // execCommand keeps the change in the native undo stack.
            document.execCommand('insertText', false, text);
        }
        // Keep the hidden field, typing indicator and auto-resize in sync.
        (isMarkdownMode ? markdownView : editor).dispatchEvent(new Event('input', { bubbles: true }));
    };

    const showPopover = function() {
        state.popoverContainer.style.display = 'block';
        state.active = true;
    };
    const hidePopover = function() {
        state.popoverContainer.style.display = 'none';
        state.popoverContainer.innerHTML = '';
        state.active = false;
        state.query = '';
    };

    return { initialize, state, hidePopover };
};

/**
 * Factory function to create a self-contained editor instance.
 */
const createEditor = function(idSuffix = '') {
    const state = {};
    let attachmentManager = null;
    let mentionManager = null;
    let emojiManager = null;

    const initialize = function() {
        const messageForm = document.getElementById(`message-form${idSuffix}`);
        if (!messageForm) return;

        const elements = {
            messageForm,
            editor: document.getElementById(`wysiwyg-editor${idSuffix}`),
            markdownView: document.getElementById(`markdown-toggle-view${idSuffix}`),
            hiddenInput: document.getElementById(`chat-message-input${idSuffix}`),
            hiddenAttachmentIds: document.getElementById(`attachment-file-ids${idSuffix}`),
            topToolbar: messageForm.querySelector('.wysiwyg-toolbar:not(.wysiwyg-toolbar-bottom)'),
            formatToggleButton: document.getElementById(`format-toggle-btn${idSuffix}`),
            sendButton: document.getElementById(`send-button${idSuffix}`),
            typingSender: document.getElementById('typing-sender'),
            blockquoteButton: messageForm.querySelector('[data-command="formatBlock"][data-value="blockquote"]'),
            emojiButton: document.getElementById(`emoji-btn${idSuffix}`),
            emojiPickerContainer: document.getElementById(`emoji-picker-container${idSuffix}`),
            emojiPicker: messageForm.querySelector('emoji-picker')
        };

        if (Object.values(elements).some(el => !el)) {
            console.error(`Editor init failed for suffix "${idSuffix}"`);
            return;
        }

        const isMarkdownMode = !!(elements.markdownView && elements.markdownView.style.display !== 'none');

        const turndownService = new TurndownService({
            headingStyle: 'atx',
            codeBlockStyle: 'fenced',
            br: '\n'
        });
        turndownService.addRule('strikethrough', {
            filter: ['del', 's', 'strike'],
            replacement: c => `~~${c}~~`
        });

        turndownService.addRule('pre', {
            filter: 'pre',
            replacement: function(content) {
                return '```\n' + content + '\n```';
            }
        });

        // Mention pills serialize as their literal "@username" — the default
        // text pipeline would escape underscores ("@josue\_sahad"), which
        // breaks the server's mention parsing for those users.
        turndownService.addRule('mentionToken', {
            filter: (node) => node.nodeName === 'SPAN' && node.classList.contains('mention-token'),
            replacement: (content, node) => node.textContent
        });

        Object.assign(state, {
            idSuffix,
            ...elements,
            turndownService,
            isMarkdownMode,
            typingTimer: null
        });

        // Pass the editor's state object to the attachment manager.
        attachmentManager = createAttachmentManager(state);
        attachmentManager.initialize();
        state.attachmentManager = attachmentManager;

        mentionManager = createMentionManager(state);
        mentionManager.initialize();

        emojiManager = createEmojiManager(state);
        emojiManager.initialize();

        const mentionButton = document.getElementById(`mention-btn${idSuffix}`);
        if (mentionButton) {
            mentionButton.addEventListener('click', () => {
                insertText('@');
                const activeInput = state.isMarkdownMode ? state.markdownView : state.editor;
                activeInput.dispatchEvent(new Event('input', {
                    bubbles: true,
                    cancelable: true
                }));
            });
        }

        setupEmojiPickerListeners();
        setupToolbarListener();
        setupInputListeners();
        setupKeydownListeners();
        setupFormListeners();
        setupToggleButtonListener();
        updateView();

        if (!state.isMarkdownMode) {
            const markdownContent = state.markdownView.value;
            if (markdownContent.trim() !== '') {
                // We use the same markdown-to-html utility as the toggle button.
                // This ensures consistent rendering and properly handles all content,
                // including mixed text and code blocks.
                htmx.ajax('POST', '/chat/utility/markdown-to-html', {
                    values: {
                        text: markdownContent
                    },
                    target: state.editor,
                    swap: 'innerHTML'
                }).then(() => {
                    // After the content is loaded, sync the hidden input and button states.
                    updateStateAndButtons();
                    // And ensure the input resizes to fit the loaded content.
                    resizeActiveInput();
                });
            }
        }
    };

    const preprocessMarkdown = function(text) {
        const lines = text.split('\n');
        const processedLines = [];
        for (let i = 0; i < lines.length; i++) {
            processedLines.push(lines[i]);
            const isQuote = lines[i].trim().startsWith('>');
            if (isQuote && (i + 1 < lines.length) && lines[i + 1].trim() !== '' && !lines[i + 1].trim().startsWith('>')) {
                processedLines.push('');
            }
        }
        return processedLines.join('\n');
    };
    const insertText = function(text) {
        const {
            editor,
            markdownView,
            isMarkdownMode
        } = state;
        if (isMarkdownMode) {
            const start = markdownView.selectionStart;
            const end = markdownView.selectionEnd;
            const currentText = markdownView.value;
            markdownView.value = currentText.substring(0, start) + text + currentText.substring(end);
            markdownView.focus();
            markdownView.selectionStart = markdownView.selectionEnd = start + text.length;
        } else {
            editor.focus();
            document.execCommand('insertText', false, text);
        }
        const activeInput = isMarkdownMode ? markdownView : editor;
        activeInput.dispatchEvent(new Event('input', {
            bubbles: true,
            cancelable: true
        }));
    };
    const focusActiveInput = function() {
        const {
            editor,
            markdownView,
            isMarkdownMode
        } = state;
        const activeInput = isMarkdownMode ? markdownView : editor;
        if (activeInput && typeof activeInput.focus === 'function') {
            setTimeout(() => activeInput.focus(), 0);
        }
    };
    const updateView = function() {
        const {
            editor,
            markdownView,
            topToolbar,
            isMarkdownMode
        } = state;
        if (isMarkdownMode) {
            editor.style.display = 'none';
            markdownView.style.display = 'block';
            if (topToolbar) topToolbar.classList.add('toolbar-hidden');
            markdownView.focus();
        } else {
            markdownView.style.display = 'none';
            editor.style.display = 'block';
            if (topToolbar) topToolbar.classList.remove('toolbar-hidden');
            editor.focus();
        }
        resizeActiveInput();
        updateSendButton();
    };
    const updateSendButton = function() {
        const {
            sendButton,
            messageForm
        } = state;
        const replyTypeInput = messageForm.querySelector('input[name="reply_type"]');
        const isQuoteReply = replyTypeInput && replyTypeInput.value === 'quote';
        // The edit composers name their action (data-send-label="Save").
        const sendText = sendButton.dataset.sendLabel || (isQuoteReply ? "Reply" : "Send");
        // Reflect the user's send-key preference, not the editor mode.
        const enterSends = document.querySelector('main.main-content').dataset.sendOnEnter !== 'false';
        if (enterSends) {
            sendButton.innerHTML = `<span>${sendText}</span><span class="send-shortcut"><i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = `${sendText} (Enter)`;
        } else {
            sendButton.innerHTML = `<span>${sendText}</span><span class="send-shortcut"><kbd>Ctrl</kbd>+<i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = `${sendText} (Ctrl+Enter)`;
        }
    };
    const resizeActiveInput = function() {
        const {
            editor,
            markdownView,
            isMarkdownMode
        } = state;
        const activeInput = isMarkdownMode ? markdownView : editor;
        setTimeout(() => {
            if (activeInput) {
                activeInput.style.height = 'auto';
                activeInput.style.height = `${activeInput.scrollHeight}px`;
            }
        }, 0);
    };
    const updateStateAndButtons = function() {
        const {
            editor,
            hiddenInput,
            turndownService,
            blockquoteButton,
            topToolbar
        } = state;
        if (!editor || !hiddenInput || !turndownService) return;
        const htmlContent = editor.innerHTML;
        // The NBSP spacers that follow mention pills become plain spaces.
        const markdownContent = turndownService.turndown(htmlContent).replace(/\u00A0/g, ' ').trim();
        hiddenInput.value = markdownContent;
        const commands = ['bold', 'italic', 'strikethrough', 'insertUnorderedList', 'insertOrderedList'];
        if (topToolbar) {
            commands.forEach(cmd => {
                const btn = topToolbar.querySelector(`[data-command="${cmd}"]`);
                if (btn) btn.classList.toggle('active', document.queryCommandState(cmd));
            });
        }
        if (blockquoteButton) {
            blockquoteButton.classList.toggle('active', isSelectionInBlockquote());
        }
    };
    const isSelectionInBlockquote = function() {
        const selection = window.getSelection();
        if (!selection.rangeCount) return false;
        let node = selection.getRangeAt(0).startContainer;
        if (node.nodeType === 3) node = node.parentNode;
        while (node && node.id !== state.editor.id) {
            if (node.nodeName === 'BLOCKQUOTE') return true;
            node = node.parentNode;
        }
        return false;
    };
    const sendTypingStatus = function(isTyping) {
        const {
            typingSender
        } = state;
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        if (activeConv) {
            const payload = {
                type: isTyping ? 'typing_start' : 'typing_stop',
                conversation_id: activeConv.dataset.conversationId
            };
            typingSender.setAttribute('hx-vals', JSON.stringify(payload));
            htmx.trigger(typingSender, 'typing-event');
        }
    };
    const setupEmojiPickerListeners = function() {
        const {
            emojiButton,
            emojiPicker,
            emojiPickerContainer
        } = state;
        if (!emojiButton || !emojiPicker || !emojiPickerContainer) return;
        emojiButton.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('[id^="emoji-picker-container"]').forEach(picker => {
                if (picker !== emojiPickerContainer) {
                    picker.style.display = 'none';
                }
            });
            const isHidden = emojiPickerContainer.style.display === 'none';
            emojiPickerContainer.style.display = isHidden ? 'block' : 'none';
        });
        emojiPicker.addEventListener('emoji-click', event => {
            insertText(event.detail.unicode);
            emojiPickerContainer.style.display = 'none';
        });
    };
    const setupToolbarListener = function() {
        if (!state.topToolbar) return;
        state.topToolbar.addEventListener('mousedown', e => {
            e.preventDefault();
            const button = e.target.closest('button');
            if (!button) return;
            if (button.dataset.value === 'blockquote') {
                const format = isSelectionInBlockquote() ? 'div' : 'blockquote';
                document.execCommand('formatBlock', false, format);
            } else {
                const {
                    command,
                    value
                } = button.dataset;
                document.execCommand(command, false, value);
            }
            state.editor.focus();
            updateStateAndButtons();
        });
    };
    // Typing indicators: one typing_start when typing begins (refreshed every
    // few seconds while it continues) and a typing_stop after a pause. A frame
    // per keystroke cost a DB check and a publish each, and fast typing drained
    // the socket's rate limit so the subscribe that followed could be dropped.
    const TYPING_REFRESH_MS = 3000;
    const noteTyping = function() {
        const now = Date.now();
        if (!state.typingSince || now - state.typingSince > TYPING_REFRESH_MS) {
            state.typingSince = now;
            sendTypingStatus(true);
        }
        clearTimeout(state.typingTimer);
        state.typingTimer = setTimeout(() => {
            state.typingSince = 0;
            sendTypingStatus(false);
        }, 1500);
    };
    const noteDraft = function() {
        if (idSuffix !== '') return;
        clearTimeout(state.draftTimer);
        state.draftTimer = setTimeout(() => DraftManager.save(activeConversationId(), getText()), 400);
    };
    const setupInputListeners = function() {
        state.editor.addEventListener('input', () => {
            updateStateAndButtons();
            resizeActiveInput();
            noteTyping();
            noteDraft();
        });
        state.markdownView.addEventListener('input', () => {
            resizeActiveInput();
            noteTyping();
            noteDraft();
        });
        // One document listener for every editor, following the focused one:
        // each editor used to add its own and never remove it, so they piled
        // up with every composer swap.
        selectionEditor = state.editor;
        selectionEditorUpdate = updateStateAndButtons;
    };
    // If the caret is in a list item, start the next item (or, from an empty
    // item, end the list) and return true; otherwise return false.
    const continueList = function() {
        if (!state.isMarkdownMode) {
            const selection = window.getSelection();
            if (!selection.rangeCount) return false;
            let node = selection.getRangeAt(0).startContainer;
            if (node.nodeType !== Node.ELEMENT_NODE) node = node.parentElement;
            const item = node && node.closest('li');
            if (!item || !state.editor.contains(item)) return false;
            // The same command plain Enter runs in a contenteditable list.
            document.execCommand('insertParagraph');
            return true;
        }
        const view = state.markdownView;
        const pos = view.selectionStart;
        if (pos !== view.selectionEnd) return false;
        const text = view.value;
        const lineStart = text.lastIndexOf('\n', pos - 1) + 1;
        const marker = /^(\s*)(?:([-*+])|(\d+)\.)\s+/.exec(text.slice(lineStart, pos));
        if (!marker) return false;
        const newline = text.indexOf('\n', pos);
        const restOfLine = text.slice(pos, newline === -1 ? text.length : newline);
        let from = pos;
        let insert;
        if (marker[0].length === pos - lineStart && restOfLine.trim() === '') {
            // An empty item ends the list: drop its marker.
            from = lineStart;
            insert = '';
        } else {
            insert = `\n${marker[1]}${marker[2] || `${Number(marker[3]) + 1}.`} `;
        }
        view.value = text.slice(0, from) + insert + text.slice(pos);
        view.selectionStart = view.selectionEnd = from + insert.length;
        view.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
    };
    const setupKeydownListeners = function() {
        const currentUserId = document.querySelector('main.main-content').dataset.currentUserId;
        const keydownHandler = (e) => {
            if (mentionManager && mentionManager.state.active) {
                return;
            }
            if (emojiManager && emojiManager.state.active) {
                return;
            }
            if (!state.isMarkdownMode && e.key === 'Enter' && !e.shiftKey) {
                const selection = window.getSelection();
                if (selection.rangeCount) {
                    const range = selection.getRangeAt(0);
                    const node = range.startContainer;
                    if (isSelectionInBlockquote() && node.textContent.trim() === '' && range.startOffset === node.textContent.length) {
                        e.preventDefault();
                        document.execCommand('formatBlock', false, 'div');
                        return;
                    }
                }
            }
            const currentContent = state.isMarkdownMode ? state.markdownView.value : state.editor.innerText;
            if (idSuffix === '' && e.key === 'ArrowUp' && currentContent.trim() === '') {
                e.preventDefault();
                // Your newest message in the open conversation (":last-of-type"
                // only matched when it was the very last element in the list).
                const own = document.querySelectorAll(`#message-list .message-container[data-user-id="${currentUserId}"]`);
                const lastUserMessage = own[own.length - 1];
                if (lastUserMessage) {
                    const editButton = lastUserMessage.querySelector('.message-toolbar button[data-action="edit"]');
                    if (editButton) htmx.trigger(editButton, 'click');
                }
                return;
            }
            if (e.key === 'Enter') {
                // Send-key behavior is a user preference (Preferences menu),
                // read live from the DOM so a change applies without reload and
                // works the same in both the plain-textarea and rich editor.
                //   enterSends true  -> Enter sends, Shift+Enter = newline
                //   enterSends false -> Ctrl/Cmd+Enter sends, Enter = newline
                const enterSends = document.querySelector('main.main-content').dataset.sendOnEnter !== 'false';
                // In a list, the new-line key starts the next item, as in
                // Slack: Shift+Enter when Enter sends, plain Enter otherwise
                // (which the rich editor already does on its own).
                const plain = !e.ctrlKey && !e.metaKey;
                const nextItemKey = enterSends
                    ? e.shiftKey && plain
                    : !e.shiftKey && plain && state.isMarkdownMode;
                if (nextItemKey && continueList()) {
                    e.preventDefault();
                    return;
                }
                const hasAttachment = state.hiddenAttachmentIds ? state.hiddenAttachmentIds.value !== '' : false;
                const content = state.isMarkdownMode ? state.markdownView.value : state.editor.innerText;
                const hasContent = content.trim() !== '' || hasAttachment;

                if (enterSends) {
                    if (!e.shiftKey) {
                        e.preventDefault();
                        if (hasContent) state.messageForm.requestSubmit();
                    }
                    // Shift+Enter falls through to the default newline.
                } else if (e.ctrlKey || e.metaKey) {
                    e.preventDefault();
                    if (hasContent) state.messageForm.requestSubmit();
                }
                // Plain Enter in Ctrl+Enter mode falls through to a newline.
            }
        };
        state.editor.addEventListener('keydown', keydownHandler);
        state.markdownView.addEventListener('keydown', keydownHandler);
    };
    // Copy the composed text (as Markdown) into the form's hidden text field.
    const syncHiddenInput = function() {
        if (state.isMarkdownMode) {
            state.hiddenInput.value = preprocessMarkdown(state.markdownView.value);
        } else {
            updateStateAndButtons();
        }
    };
    const setupFormListeners = function() {
        state.messageForm.addEventListener('submit', (e) => {
            syncHiddenInput();
            const hasText = state.hiddenInput.value.trim() !== '';
            const hasAttachment = state.hiddenAttachmentIds ? state.hiddenAttachmentIds.value !== '' : false;
            if (!hasText && !hasAttachment) {
                e.preventDefault();
                return;
            }
            clearTimeout(state.typingTimer);
            sendTypingStatus(false);
        });
        // Web messages POST over HTTP (the socket is push-only now). Rewrite
        // the placeholder path to the active conversation, inject the composed
        // message text, and cancel the request when there's nothing to send or
        // no conversation is open. Guarded to 'post' so the in-form hx-get
        // buttons (poll/emoji/mention), whose events bubble up to the form,
        // pass through untouched.
        state.messageForm.addEventListener('htmx:configRequest', (e) => {
            // An edit composer saves with PUT. htmx may have read the form's
            // fields before the submit listener above synced the editor into
            // them (htmx binds its own listener first when the composer is
            // swapped in), so the text goes into the request here.
            if (e.detail.verb === 'put' && e.detail.elt === state.messageForm) {
                syncHiddenInput();
                e.detail.parameters[state.hiddenInput.name] = state.hiddenInput.value;
                return;
            }
            if (e.detail.verb !== 'post') return;
            syncHiddenInput();
            const text = state.hiddenInput.value;
            const hasText = text.trim() !== '';
            const hasAttachment = state.hiddenAttachmentIds ? state.hiddenAttachmentIds.value !== '' : false;
            if (!hasText && !hasAttachment) {
                e.preventDefault();
                return;
            }
            const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
            const convId = activeConv && activeConv.dataset.conversationId;
            if (!convId) {
                e.preventDefault();
                ToastManager.show('Not connected', 'Could not determine the current conversation. Please reload the page.', 'warning');
                return;
            }
            e.detail.path = '/chat/conversations/' + encodeURIComponent(convId) + '/messages';
            e.detail.parameters['chat_message'] = text;
            clearTimeout(state.typingTimer);
            state.typingSince = 0;
            sendTypingStatus(false);

            // Show it now, and clear the composer now: text typed while the
            // request is in flight is the next message, not something the
            // response gets to wipe.
            const clientId = newClientId();
            e.detail.parameters['client_id'] = clientId;
            const values = { chat_message: text, client_id: clientId };
            ['attachment_file_ids', 'parent_message_id', 'reply_type', 'quoted_message_id'].forEach((name) => {
                const value = e.detail.parameters[name];
                if (value) values[name] = value;
            });
            const isThread = idSuffix.startsWith('-thread-');
            const attachmentIds = state.hiddenAttachmentIds ? state.hiddenAttachmentIds.value : '';
            PendingMessages.add({
                clientId,
                text,
                attachments: attachmentIds ? attachmentIds.split(',').filter(Boolean).length : 0,
                listSelector: isThread ? `#thread-replies-list-${idSuffix.split('-').pop()}` : '#message-list',
                path: e.detail.path,
                values,
            });
            if (idSuffix === '') DraftManager.clear(convId);
            clearDraftInput();
        });
        state.messageForm.addEventListener('htmx:afterRequest', (e) => {
            const cfg = e.detail.requestConfig;
            // Only react to this form's own message POST, not the bubbling
            // hx-get requests from toolbar buttons inside the form. (The
            // optimistic copy's outcome is handled document-wide, so a Retry
            // goes through the same path.)
            if (!cfg || cfg.verb !== 'post' || !String(cfg.path).includes('/messages')) return;
            if (!e.detail.successful) return;
            const replyType = state.messageForm.querySelector('input[name="reply_type"]');
            const isQuoteReply = replyType && replyType.value === 'quote';
            if (idSuffix === '' && isQuoteReply) {
                // Back to the plain composer, carrying anything typed since.
                DraftManager.save(activeConversationId(), getText());
                htmx.ajax('GET', '/chat/input/default', {
                    target: '#chat-input-container',
                    swap: 'outerHTML'
                });
            }
        });
    };
    // The composer's content as Markdown (what would be sent).
    const getText = function() {
        if (state.isMarkdownMode) return state.markdownView.value;
        updateStateAndButtons();
        return state.hiddenInput.value;
    };
    // Replace the composer's content with Markdown text.
    const setText = function(text) {
        const { editor, markdownView, hiddenInput } = state;
        markdownView.value = text || '';
        hiddenInput.value = text || '';
        if (state.isMarkdownMode || !text) {
            if (!text) editor.innerHTML = '';
            resizeActiveInput();
            return Promise.resolve();
        }
        // Rendered (and sanitized) server-side, as when the composer loads.
        return htmx.ajax('POST', '/chat/utility/markdown-to-html', {
            values: { text },
            target: editor,
            swap: 'innerHTML'
        }).then(() => {
            updateStateAndButtons();
            resizeActiveInput();
        });
    };
    // Empty the composer, attachments included.
    const clearDraftInput = function() {
        const { editor, markdownView, hiddenInput } = state;
        editor.innerHTML = '';
        markdownView.value = '';
        hiddenInput.value = '';
        if (attachmentManager && typeof attachmentManager.reset === 'function') {
            attachmentManager.reset();
        }
        resizeActiveInput();
    };

    const setupToggleButtonListener = function() {
        state.formatToggleButton.addEventListener('click', () => {
            const {
                editor,
                markdownView,
                isMarkdownMode,
                turndownService
            } = state;
            if (isMarkdownMode) {
                const markdownContent = markdownView.value;
                if (markdownContent.trim() !== '') {
                    htmx.ajax('POST', '/chat/utility/markdown-to-html', {
                        values: {
                            text: markdownContent
                        },
                        target: editor,
                        swap: 'innerHTML'
                    }).then(() => {
                        updateStateAndButtons();
                    });
                } else {
                    editor.innerHTML = '';
                }
            } else {
                const htmlContent = editor.innerHTML;
                if (editor.innerText.trim() !== '') {
                    markdownView.value = turndownService.turndown(htmlContent);
                } else {
                    markdownView.value = '';
                }
            }
            state.isMarkdownMode = !state.isMarkdownMode;
            updateView();
            if (idSuffix === '') {
                const wysiwygIsEnabled = !state.isMarkdownMode;
                htmx.ajax('PUT', '/chat/user/preference/wysiwyg', {
                    values: {
                        'wysiwyg_enabled': wysiwygIsEnabled
                    },
                    swap: 'none'
                });
            }
        });
    };

    // This is the crucial change. We now return the manager instance.
    return {
        initialize,
        focusActiveInput,
        state,
        updateStateAndButtons,
        get attachmentManager() { return attachmentManager; },
        getText,
        setText,
        clear: clearDraftInput,
    };
};

// See setupInputListeners: the one selectionchange listener all editors share.
let selectionEditor = null;
let selectionEditorUpdate = null;
document.addEventListener('selectionchange', () => {
    if (selectionEditor && document.activeElement === selectionEditor && selectionEditorUpdate) {
        selectionEditorUpdate();
    }
});


// --- Event Listeners to initialize editors ---
const emojiPickerReady = customElements.whenDefined('emoji-picker');

// Defensive: every <emoji-picker> in this app must use the locally-vendored
// emoji data bundle, never the library's hardcoded jsdelivr default. The
// templates set `data-source="/js/local/emoji.json"` explicitly, but if any
// future template forgets it (or a popover injects a picker dynamically),
// this fallback ensures we still don't reach out to a CDN. The CSP would
// block the CDN fetch anyway, but the picker would silently fail to load
// any emojis — set the attribute *before* the element connects so its
// initial fetch points at our local bundle.
const LOCAL_EMOJI_DATA_SOURCE = '/js/local/emoji.json';

function _ensureLocalEmojiDataSource(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === 'EMOJI-PICKER' && !node.hasAttribute('data-source')) {
        node.setAttribute('data-source', LOCAL_EMOJI_DATA_SOURCE);
    }
    // Also fix up any descendants — covers HTMX swaps that bring in a
    // template fragment containing a picker.
    if (node.querySelectorAll) {
        node.querySelectorAll('emoji-picker:not([data-source])').forEach((el) => {
            el.setAttribute('data-source', LOCAL_EMOJI_DATA_SOURCE);
        });
    }
}

// Patch any pickers already in the DOM at script load time.
_ensureLocalEmojiDataSource(document.body);

// Watch for new pickers added by HTMX swaps, popover injections, etc.
new MutationObserver((mutations) => {
    for (const m of mutations) {
        for (const added of m.addedNodes) {
            _ensureLocalEmojiDataSource(added);
        }
    }
}).observe(document.body, { childList: true, subtree: true });

// Every element matching `selector` in a newly loaded root, the root included.
function _within(root, selector) {
    if (!root || !root.querySelectorAll) return [];
    const found = [...root.querySelectorAll(selector)];
    if (root.matches && root.matches(selector)) found.unshift(root);
    return found;
}

// Swapped-in partials carry no scripts (the page CSP allows none inline);
// they mark what needs setting up instead, and this runs on every htmx load
// (the page itself, then each swap). Composers: <form data-editor="<suffix>">.
htmx.onLoad((root) => {
    _within(root, 'form[data-editor]').forEach((form) => initializeEditor(form.dataset.editor));
    _within(root, '#create-poll-form').forEach((form) => PollForm.init(form));
});

function initializeEditor(idSuffix) {
    emojiPickerReady.then(() => {
        // One editor per form: a second set of listeners on the same form
        // (each clearing the composer on send) made the second one find it
        // empty and cancel the request.
        const form = document.getElementById(`message-form${idSuffix}`);
        if (!form || form.dataset.editorReady) return;
        form.dataset.editorReady = '1';
        const editorInstance = createEditor(idSuffix);
        editorInstance.initialize();

        // If this is the main chat input, store its manager and set up drag/drop listeners.
        if (idSuffix === '') {
            window.mainEditor = editorInstance;
            window.mainAttachmentManager = editorInstance.state.attachmentManager;
            // A fresh composer (page load, or back from a reply/edit) picks up
            // the open conversation's draft, unless it arrived with text of
            // its own (a quote-reply carrying the previous draft).
            const draft = DraftManager.get(activeConversationId());
            if (draft && editorInstance.state.markdownView && !editorInstance.getText().trim()) {
                editorInstance.setText(draft);
            }
            ComposerPlaceholder.update();

            // Attach drag-drop listeners only AFTER the main editor is ready.
            // Use a guard to ensure this only runs once.
            if (!window.dragDropInitialized) {
                const mainContent = document.querySelector('main.main-content');
                if (mainContent) {
                    mainContent.addEventListener('dragenter', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        mainContent.classList.add('drag-over');
                    });
                    mainContent.addEventListener('dragover', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                    });
                    mainContent.addEventListener('dragleave', (e) => {
                        if (e.relatedTarget && mainContent.contains(e.relatedTarget)) {
                            return;
                        }
                        e.preventDefault();
                        e.stopPropagation();
                        mainContent.classList.remove('drag-over');
                    });
                    mainContent.addEventListener('drop', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        mainContent.classList.remove('drag-over');
                        const files = e.dataTransfer.files;
                        if (files.length > 0 && window.mainAttachmentManager) {
                            window.mainAttachmentManager.processAndUploadFiles(files);
                        } else if (!window.mainAttachmentManager) {
                             console.error("Main attachment manager is not available on drop.");
                             ToastManager.show('Error', 'Could not process dropped files.', 'danger');
                        }
                    });
                    window.dragDropInitialized = true;
                }
            }
        }

        // Only auto-focus if it's a thread input.
        if (idSuffix && idSuffix.startsWith('-thread-')) {
            editorInstance.focusActiveInput();
        }
    });
}

// --- Create-poll modal (partials/create_poll_modal.html) ---
// Posts to the open conversation; option rows are added from the form's
// <template> and removed in place, up to data-max-options.
const PollForm = {
    init(form) {
        const target = form.querySelector('#poll-conversation-id-input');
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        if (target && activeConv) target.value = activeConv.dataset.conversationId;
        PollForm.refresh(form);
    },
    refresh(form) {
        const rows = form.querySelectorAll('#poll-options-container .poll-option-row');
        rows.forEach((row, i) => {
            const input = row.querySelector('input');
            if (input) input.placeholder = `Option ${i + 1}`;
        });
        const max = parseInt(form.dataset.maxOptions, 10) || 10;
        const add = form.querySelector('[data-poll-add-option]');
        if (add) add.classList.toggle('d-none', rows.length >= max);
    },
};
document.body.addEventListener('click', (evt) => {
    const form = evt.target.closest('#create-poll-form');
    if (!form) return;
    if (evt.target.closest('[data-poll-add-option]')) {
        const template = form.querySelector('#poll-option-template');
        const container = form.querySelector('#poll-options-container');
        if (!template || !container) return;
        container.appendChild(template.content.cloneNode(true));
        PollForm.refresh(form);
        const inputs = container.querySelectorAll('input');
        if (inputs.length) inputs[inputs.length - 1].focus();
        return;
    }
    const remove = evt.target.closest('.remove-poll-option-btn');
    if (remove) {
        remove.closest('.poll-option-row').remove();
        PollForm.refresh(form);
    }
});

// --- Behaviors partials declare with data attributes ---
// (What used to be hx-on / onchange attributes, which need 'unsafe-eval' or
// 'unsafe-inline'.)

// Copy a message's text: the button holds it in a hidden .copy-source input.
document.body.addEventListener('click', (evt) => {
    const btn = evt.target.closest('[data-copy-message]');
    if (!btn) return;
    const source = btn.querySelector('.copy-source');
    if (!source || !navigator.clipboard) return;
    navigator.clipboard.writeText(source.value).then(() => {
        const icon = btn.querySelector('i');
        if (!icon) return;
        icon.className = 'bi bi-check2';
        setTimeout(() => { icon.className = 'bi bi-clipboard'; }, 2000);
    }).catch(() => ToastManager.show('Copy failed', 'Your browser blocked access to the clipboard.', 'danger'));
});

// Picking someone in the new-message modal opens the DM; focus its composer
// once the modal has closed.
document.body.addEventListener('click', (evt) => {
    if (evt.target.closest('[data-focus-composer]')) window.focusChatInputAfterModalClose = true;
});

// An edit composer, once its edit is saved, swaps itself back for the plain
// composer: <form data-after-save="<GET url>" data-after-save-target="<selector>">.
// Only the form's own save counts, not requests from buttons inside it.
document.body.addEventListener('htmx:afterRequest', (evt) => {
    const form = evt.detail.elt;
    if (!evt.detail.successful || !form || !form.matches || !form.matches('form[data-after-save]')) return;
    const cfg = evt.detail.requestConfig;
    if (cfg && cfg.verb === 'get') return;
    htmx.ajax('GET', form.dataset.afterSave, {
        target: form.dataset.afterSaveTarget,
        swap: 'outerHTML',
    });
});

// The notification-sound select plays the sound it's set to.
document.body.addEventListener('change', (evt) => {
    const select = evt.target.closest('[data-preview-sound]');
    if (select) previewNotificationSound(select.value);
});

// --- Image Carousel Manager ---
const ImageCarouselManager = { /* ... */ };
(function() {
    const mgr = ImageCarouselManager;
    mgr.modalEl = null;
    mgr.bootstrapModal = null;
    mgr.initialize = function() {
        mgr.modalEl = document.getElementById('image-carousel-modal');
        if (!mgr.modalEl) return;
        mgr.bootstrapModal = new bootstrap.Modal(mgr.modalEl);
        document.body.addEventListener('click', mgr.handleImageClick.bind(mgr));
        mgr.modalEl.addEventListener('shown.bs.modal', () => {
            const c = mgr.modalEl.querySelector('#messageImageCarousel');
            if (c) c.focus();
        });
        mgr.modalEl.addEventListener('hidden.bs.modal', mgr.clearCarousel.bind(mgr));
    };
    mgr.handleImageClick = function(e) {
        const link = e.target.closest('a[data-bs-toggle="modal"][data-bs-target="#image-carousel-modal"]');
        if (!link) return;
        e.preventDefault();
        const messageId = link.dataset.messageId;
        const startIndex = parseInt(link.dataset.startIndex, 10);
        const messageContainer = document.getElementById(`message-${messageId}`);
        if (!messageContainer) return;
        try {
            const attachmentsData = JSON.parse(messageContainer.dataset.attachments);
            if (attachmentsData && attachmentsData.length > 0) {
                mgr.buildAndShowCarousel(attachmentsData, startIndex);
            }
        } catch (err) {
            console.error("Could not parse attachment data:", err);
        }
    };
    mgr.buildAndShowCarousel = function(attachments, startIndex) {
        const carouselInner = document.createElement('div');
        carouselInner.className = 'carousel-inner';
        attachments.forEach((attachment, index) => {
            const itemDiv = document.createElement('div');
            itemDiv.className = index === startIndex ? 'carousel-item active' : 'carousel-item';
            const img = document.createElement('img');
            img.src = attachment.url;
            img.alt = attachment.filename;
            itemDiv.appendChild(img);
            carouselInner.appendChild(itemDiv);
        });
        const carouselHTML = `<div id="messageImageCarousel" class="carousel slide h-100 w-100" data-bs-ride="carousel" data-bs-keyboard="true" tabindex="-1"> ${carouselInner.outerHTML} <button class="carousel-control-prev" type="button" data-bs-target="#messageImageCarousel" data-bs-slide="prev"><span class="carousel-control-prev-icon" aria-hidden="true"></span><span class="visually-hidden">Previous</span></button><button class="carousel-control-next" type="button" data-bs-target="#messageImageCarousel" data-bs-slide="next"><span class="carousel-control-next-icon" aria-hidden="true"></span><span class="visually-hidden">Next</span></button></div>`;
        mgr.modalEl.querySelector('.modal-body').innerHTML = carouselHTML;
        const carouselEl = mgr.modalEl.querySelector('#messageImageCarousel');
        new bootstrap.Carousel(carouselEl);
        mgr.bootstrapModal.show();
    };
    mgr.clearCarousel = function() {
        mgr.modalEl.querySelector('.modal-body').innerHTML = '<div class="spinner-border text-light"></div>';
    };
})();

// --- 2. GENERAL PAGE-LEVEL LOGIC ---
document.addEventListener('DOMContentLoaded', () => {
    ImageCarouselManager.initialize();
    NotificationManager.initialize();
    ToastManager.initialize();
    FaviconManager.initialize();

    // --- AUDIO PRIMING LOGIC ---
    const primeAudio = () => {
        const silentSound = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";
        const audio = new Audio(silentSound);
        audio.play().then(() => {
            console.log("Audio context primed successfully.");
            document.body.removeEventListener('click', primeAudio);
        }).catch(error => {
            console.warn("Could not prime audio on first click:", error);
        });
    };
    document.body.addEventListener('click', primeAudio, { once: true });

    // Show author-only toolbar actions (edit, delete) on the viewer's own
    // messages. Added through the CSSOM because the rule needs this viewer's
    // id; an inline <style> in the page would need 'unsafe-inline'.
    (function showOwnMessageActions() {
        const main = document.querySelector('main.main-content');
        const userId = main && main.dataset.currentUserId;
        const sheet = [...document.styleSheets].find((s) => s.href && /\/css\/style\.css/.test(s.href));
        if (!userId || !sheet) return;
        const id = (window.CSS && CSS.escape) ? CSS.escape(userId) : userId;
        try {
            sheet.insertRule(
                `.message-container[data-user-id="${id}"] .message-toolbar .author-only-action { display: inline-block !important; }`,
                sheet.cssRules.length,
            );
        } catch (e) { /* a cross-origin or unparsable sheet: leave the actions hidden */ }
    })();

    // The search box searches as you type (hx-get); Enter must not submit the form.
    const searchForm = document.getElementById('global-search-form');
    if (searchForm) searchForm.addEventListener('submit', (e) => e.preventDefault());

    const searchInput = document.getElementById('global-search-input');
    const searchOverlay = document.getElementById('search-results-overlay');
    const searchWrapper = document.querySelector('.global-search-wrapper');
    const channelSidebar = document.querySelector('.channel-sidebar');
    const showCompactSearch = () => {
        if (!searchWrapper) return;
        const rect = searchWrapper.getBoundingClientRect();
        searchOverlay.classList.add('compact');
        Object.assign(searchOverlay.style, {
            position: 'fixed',
            top: `${rect.bottom}px`,
            left: `${rect.left}px`,
            width: `${rect.width}px`,
            display: 'flex',
        });
    };
    const showFullSearch = () => {
        searchOverlay.classList.remove('compact');
        searchOverlay.removeAttribute('style');
        searchOverlay.style.display = 'flex';
        searchOverlay.style.flexDirection = 'column';
    };
    const hideSearch = () => {
        if (!searchOverlay || !searchInput) return;
        searchOverlay.classList.remove('compact');
        searchOverlay.style.display = 'none';
        searchInput.value = '';
        htmx.trigger(searchInput, 'htmx:abort');
    };
    if (searchInput && searchOverlay) {
        searchInput.addEventListener('input', () => {
            if (searchInput.value.trim()) {
                showCompactSearch();
            } else {
                searchOverlay.style.display = 'none';
            }
        });
        // native `search` event fires on Enter (and the input's native clear-X)
        searchInput.addEventListener('search', () => {
            if (searchInput.value.trim()) {
                showFullSearch();
            } else {
                hideSearch();
            }
        });
    }
    if (searchOverlay) {
        searchOverlay.addEventListener('click', (e) => {
            // The close (×) button is swapped in with the results, so it isn't
            // present when listeners bind at load — delegate from the overlay.
            // This also makes the Escape handler work, since it fires a
            // synthetic click on #close-search-btn.
            if (e.target.closest('#close-search-btn')) {
                hideSearch();
                return;
            }
            if (e.target.closest('div.search-result-item')) {
                hideSearch();
            }
        });
    }
    if (channelSidebar) {
        channelSidebar.addEventListener('click', (e) => {
            if (searchOverlay && e.target.closest('a[hx-get]') && searchOverlay.style.display !== 'none') {
                hideSearch();
            }
        });
    }
    // --- Keyboard navigation -------------------------------------------------
    //   Ctrl/Cmd+K          quick switcher (Enter with no match searches messages)
    //   Alt+Up / Alt+Down   previous / next conversation in the sidebar
    //   Alt+Shift+Up/Down   previous / next unread conversation
    const sidebarAnchors = () => [...document.querySelectorAll('#channel-list .sidebar-item-link, #dm-list .sidebar-item-link')];
    const isUnreadAnchor = (a) => !!a.querySelector('.fw-bold, .badge');
    const anchorName = (a) => {
        const label = a.querySelector('[id^="link-"]') || a;
        return label.textContent.replace(/\s+/g, ' ').trim();
    };
    const openAnchor = (a) => {
        if (!a) return;
        htmx.trigger(a, 'click');
        if (window.mainEditor) window.mainEditor.focusActiveInput();
    };
    const stepConversation = (direction, unreadOnly) => {
        const anchors = sidebarAnchors();
        if (!anchors.length) return;
        const current = anchors.findIndex((a) => a.classList.contains('active'));
        for (let step = 1; step <= anchors.length; step++) {
            const index = ((current + direction * step) % anchors.length + anchors.length) % anchors.length;
            const candidate = anchors[index];
            if (!unreadOnly || isUnreadAnchor(candidate)) {
                openAnchor(candidate);
                return;
            }
        }
    };

    const QuickSwitcher = {
        el: document.getElementById('quick-switcher'),
        input: document.getElementById('quick-switcher-input'),
        results: document.getElementById('quick-switcher-results'),
        items: [],
        selected: 0,
        returnFocus: null,
        isOpen() { return !!this.el && !this.el.hidden; },
        toggle() { if (this.isOpen()) this.close(); else this.open(); },
        open() {
            if (!this.el) return;
            this.returnFocus = document.activeElement;
            this.el.hidden = false;
            this.input.value = '';
            this.render();
            this.input.focus();
        },
        close({ restoreFocus = true } = {}) {
            if (!this.isOpen()) return;
            this.el.hidden = true;
            if (restoreFocus && this.returnFocus && typeof this.returnFocus.focus === 'function') this.returnFocus.focus();
        },
        render() {
            const typed = this.input.value.trim();
            const query = typed.toLowerCase().replace(/^#\s*/, '');
            let candidates = sidebarAnchors().map((anchor, order) => ({
                anchor, order, name: anchorName(anchor), unread: isUnreadAnchor(anchor),
            }));
            if (query) {
                candidates = candidates
                    .map((c) => {
                        const at = c.name.toLowerCase().replace(/^#\s*/, '').indexOf(query);
                        return { ...c, score: at === 0 ? 0 : at > 0 ? 1 : -1 };
                    })
                    .filter((c) => c.score >= 0)
                    .sort((a, b) => a.score - b.score || a.order - b.order);
            } else {
                // Unread first; otherwise the sidebar's own order.
                candidates.sort((a, b) => (b.unread - a.unread) || a.order - b.order);
            }
            this.items = candidates.slice(0, 12);
            this.selected = 0;
            this.results.textContent = '';
            this.items.forEach((c, i) => {
                const li = document.createElement('li');
                li.className = `list-group-item list-group-item-action quick-switcher-item${c.unread ? ' fw-bold' : ''}`;
                li.id = `quick-switcher-option-${i}`;
                li.setAttribute('role', 'option');
                li.textContent = c.name;
                li.addEventListener('mousedown', (e) => {
                    e.preventDefault();
                    this.choose(i);
                });
                this.results.appendChild(li);
            });
            if (!this.items.length) {
                const li = document.createElement('li');
                li.className = 'list-group-item text-muted quick-switcher-empty';
                li.textContent = typed ? `No conversation matches. Press Enter to search messages for “${typed}”.` : 'No conversations yet.';
                this.results.appendChild(li);
            }
            this.highlight();
        },
        highlight() {
            [...this.results.querySelectorAll('.quick-switcher-item')].forEach((li, i) => {
                const on = i === this.selected;
                li.classList.toggle('active', on);
                li.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            if (this.items.length) this.input.setAttribute('aria-activedescendant', `quick-switcher-option-${this.selected}`);
            else this.input.removeAttribute('aria-activedescendant');
        },
        move(delta) {
            if (!this.items.length) return;
            this.selected = (this.selected + delta + this.items.length) % this.items.length;
            this.highlight();
        },
        choose(index) {
            const item = this.items[index];
            const typed = this.input.value.trim();
            this.close({ restoreFocus: false });
            if (item) {
                openAnchor(item.anchor);
            } else if (typed && searchInput) {
                searchInput.value = typed;
                searchInput.dispatchEvent(new Event('search', { bubbles: true }));
            }
        },
    };
    window.QuickSwitcher = QuickSwitcher;
    if (QuickSwitcher.el) {
        QuickSwitcher.input.addEventListener('input', () => QuickSwitcher.render());
        QuickSwitcher.input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); QuickSwitcher.move(1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); QuickSwitcher.move(-1); }
            else if (e.key === 'Enter') { e.preventDefault(); QuickSwitcher.choose(QuickSwitcher.selected); }
            else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); QuickSwitcher.close(); }
        });
        // A click on the dimmed backdrop closes it.
        QuickSwitcher.el.addEventListener('mousedown', (e) => {
            if (e.target === QuickSwitcher.el) QuickSwitcher.close();
        });
    }

    document.body.addEventListener('keydown', (e) => {
        const mod = e.ctrlKey || e.metaKey;
        if (mod && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            QuickSwitcher.toggle();
            return;
        }
        if (e.altKey && !mod && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
            e.preventDefault();
            stepConversation(e.key === 'ArrowDown' ? 1 : -1, e.shiftKey);
        }
    });

    // --- Confirmations ---------------------------------------------------------
    // hx-confirm (delete a message, leave a conversation...) asks in the app's
    // modal: see ConfirmDialog in ui.js.

    // --- Reactions -----------------------------------------------------------------
    // Clicking a reaction pill flips it at once; the server's copy of the
    // reactions (OOB) replaces it a moment later with the real counts.
    document.body.addEventListener('htmx:beforeRequest', (evt) => {
        const pill = evt.detail.elt && evt.detail.elt.closest && evt.detail.elt.closest('.reaction-pill');
        if (!pill) return;
        const mine = pill.classList.toggle('user-reacted');
        const count = pill.querySelector('.reaction-count');
        if (count) {
            const next = Math.max(0, (parseInt(count.textContent, 10) || 0) + (mine ? 1 : -1));
            count.textContent = String(next);
            if (next === 0) pill.classList.add('reaction-removing');
        }
    });

    // Cancelling a quote-reply keeps what was typed in it as the draft.
    document.body.addEventListener('click', (e) => {
        if (e.target.closest('#chat-input-container .btn-close[aria-label="Cancel Reply"]') && window.mainEditor) {
            DraftManager.save(activeConversationId(), window.mainEditor.getText());
        }
    });

    // --- Double-click to Edit Logic ---
    document.body.addEventListener('dblclick', function(e) {
        // Find the message container that was double-clicked.
        const messageContainer = e.target.closest('.message-container');
        if (!messageContainer) {
            return; // Exit if the click wasn't on or inside a message.
        }

        // Get the author's ID from the message's data attribute.
        const messageAuthorId = messageContainer.dataset.userId;
        // Get the current logged-in user's ID.
        const mainContent = document.querySelector('main.main-content');
        const currentUserId = mainContent ? mainContent.dataset.currentUserId : null;

        // Only proceed if the current user is the author of the message.
        if (currentUserId && messageAuthorId === currentUserId) {
            // Find the specific edit button for this message within its toolbar.
            const editButton = messageContainer.querySelector('.message-toolbar button[data-action="edit"]');

            if (editButton) {
                // Prevent the default double-click behavior (which is to select text).
                e.preventDefault();
                // Programmatically trigger the HTMX 'click' event on the edit button.
                // This reuses all our existing backend logic for loading the editor.
                htmx.trigger(editButton, 'click');
            }
        }
    });

    document.body.addEventListener('htmx:configRequest', function(evt) {
        const trigger = evt.detail.elt;
        if (!trigger) return;
        const targetId = evt.detail.target.id;
        if (targetId === 'chat-input-container' && trigger.dataset.action === 'reply') {
            let draftContent = '';
            if (window.mainEditor && window.mainEditor.state) {
                if (window.mainEditor.state.isMarkdownMode) {
                    draftContent = window.mainEditor.state.markdownView.value;
                } else {
                    window.mainEditor.updateStateAndButtons();
                    draftContent = window.mainEditor.state.hiddenInput.value;
                }
            }
            if (draftContent.trim() !== '') {
                evt.detail.parameters['draft'] = draftContent;
            }
        }
    });

    document.body.addEventListener('htmx:responseError', function(evt) {
        // We only want to show a toast for unexpected errors, not for things
        // like the websocket connection, which has its own handler.
        if (evt.detail.target.tagName === 'MAIN' && evt.detail.target.hasAttribute('ws-connect')) {
            return;
        }

        // A failed send is shown on the message itself (Retry / Edit / Discard).
        const cfg = evt.detail.requestConfig;
        if (cfg && cfg.formData && cfg.formData.get && cfg.formData.get('client_id')) return;

        ToastManager.show('Something went wrong', friendlyErrorText(evt.detail.xhr), 'danger');
    });

    // Every message POST carrying a client_id (the composer's, or a Retry)
    // settles its optimistic copy here.
    document.body.addEventListener('htmx:afterRequest', (evt) => {
        const cfg = evt.detail.requestConfig;
        if (!cfg || cfg.verb !== 'post' || !cfg.formData || !cfg.formData.get) return;
        const clientId = cfg.formData.get('client_id');
        if (!clientId) return;
        if (evt.detail.successful) {
            // The response's OOB swap normally replaced it already.
            PendingMessages.resolve(clientId);
        } else {
            const xhr = evt.detail.xhr;
            const offline = !xhr || !xhr.status;
            PendingMessages.fail(clientId, offline ? 'Not sent: no connection.' : `Not sent: ${friendlyErrorText(xhr)}`);
        }
    });

    document.body.addEventListener('click', (evt) => {
        const btn = evt.target.closest('[data-pending-action]');
        if (!btn) return;
        const pending = btn.closest('.pending-message');
        if (!pending) return;
        const { clientId } = pending.dataset;
        const action = btn.dataset.pendingAction;
        if (action === 'retry') PendingMessages.retry(clientId);
        else if (action === 'edit') PendingMessages.edit(clientId);
        else if (action === 'discard') PendingMessages.discard(clientId);
    });

    // An OOB fragment with no target is normal here: a thread reply for a
    // panel that isn't open, a badge for a conversation not in the sidebar,
    // an append dropped because another conversation is on screen. Nothing to
    // report. (This used to aim a "UI Sync Error" toast at evt.detail.target,
    // which htmx 2 doesn't set, so it only logged on every such frame.)

    const connectionStatusBar = document.getElementById('connection-status-bar');
    let isReconnecting = false;
    document.body.addEventListener('htmx:wsError', function(evt) {
        if (!isReconnecting) {
            console.warn("WebSocket Connection Error. Will retry.");
            isReconnecting = true;
            if (connectionStatusBar) {
                connectionStatusBar.style.display = 'block';
            }
        }
    });
    // Fetch anything newer than what's on screen for the open conversation.
    // Called after a reconnect (pub/sub is at-most-once, so a socket that was
    // down received none of it) and whenever the server acknowledges a
    // subscribe (broadcasts between the pane's render and the subscribe went
    // nowhere). Skipped while the pane shows an older page with a newer-page
    // loader below it: appending the newest messages there would leave a gap.
    function backfillMessages(convId) {
        if (!convId || document.querySelector('#message-list .newer-message-loader')) return;
        let lastId = 0;
        document
            .querySelectorAll('#message-list .message-container[id^="message-"]')
            .forEach((el) => {
                const n = parseInt(el.id.slice('message-'.length), 10);
                if (!Number.isNaN(n) && n > lastId) lastId = n;
            });
        // OOB fragments in the response apply themselves; the dedupe guard in
        // htmx:oobAfterSwap protects against a message that also arrived live.
        htmx.ajax('GET',
            `/chat/conversations/${encodeURIComponent(convId)}/messages/since/${lastId}`,
            { source: document.body, swap: 'none' }
        ).catch(() => {});
    }

    // Re-emit sidebar rows and badges for anything that changed while the
    // socket was down, including DMs started in the meantime.
    function catchUpSidebar() {
        htmx.ajax('GET', '/chat/sidebar/unreads',
            { source: document.body, swap: 'none' }
        ).catch(() => {});
    }

    // Application-level heartbeat. A socket can die without the browser
    // noticing (laptop sleep, Wi-Fi change, a NAT dropping an idle flow): it
    // stays OPEN, nothing arrives, and the page looks fine until a refresh. So
    // ping every 25s and, if no frame at all comes back within 10s, treat the
    // socket as dead and replace it. Judged by "has anything arrived since the
    // ping" rather than a frame's age, so a throttled background tab (timers
    // firing once a minute) doesn't mistake its own slowness for a dead
    // socket.
    const HEARTBEAT_INTERVAL_MS = 25000;
    const PONG_TIMEOUT_MS = 10000;
    const WAKE_PONG_TIMEOUT_MS = 5000;
    let liveSocket = null;
    let framesSincePing = 0;
    let pongTimer = null;

    function sendPing(timeoutMs = PONG_TIMEOUT_MS) {
        if (!liveSocket) return;
        try {
            liveSocket.send(JSON.stringify({ type: 'ping' }));
        } catch (e) {
            return;
        }
        framesSincePing = 0;
        clearTimeout(pongTimer);
        pongTimer = setTimeout(checkPong, timeoutMs);
    }

    function checkPong() {
        if (!liveSocket || framesSincePing > 0) return;
        console.warn('No reply to the WebSocket heartbeat; reconnecting.');
        liveSocket = null;
        isReconnecting = true;
        if (connectionStatusBar) connectionStatusBar.style.display = 'block';
        if (typeof window.d8WsReconnectNow === 'function') window.d8WsReconnectNow(true);
    }

    setInterval(() => sendPing(), HEARTBEAT_INTERVAL_MS);

    document.body.addEventListener('htmx:wsBeforeMessage', function() {
        framesSincePing++;
    });

    // If too many messages were missed to append safely, the catch-up endpoint
    // sets X-D8-Catchup: truncated — reload the pane instead of a partial gap.
    document.body.addEventListener('htmx:afterRequest', function(evt) {
        const cfg = evt.detail.requestConfig;
        if (!cfg || !String(cfg.path).includes('/messages/since/')) return;
        if (evt.detail.successful && evt.detail.xhr &&
            evt.detail.xhr.getResponseHeader('X-D8-Catchup') === 'truncated') {
            const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
            const convId = activeConv && activeConv.dataset.conversationId;
            const link = convId && document.getElementById('link-' + convId);
            if (link) htmx.trigger(link, 'click');
            else window.location.reload();
        }
    });

    document.body.addEventListener('htmx:wsOpen', function(evt) {
        console.log("WebSocket Connection Opened.");
        const wasReconnecting = isReconnecting;
        if (isReconnecting && connectionStatusBar) {
            connectionStatusBar.classList.replace('bg-warning', 'bg-success');
            connectionStatusBar.innerHTML = 'Connection restored.';
            setTimeout(() => {
                connectionStatusBar.style.display = 'none';
                connectionStatusBar.classList.replace('bg-success', 'bg-warning');
                connectionStatusBar.innerHTML = `<div class="spinner-border spinner-border-sm me-2"></div> Connection lost. Attempting to reconnect...`;
            }, 2000);
        }
        liveSocket = evt.detail && evt.detail.socketWrapper;
        clearTimeout(pongTimer);
        const convId = activeConversationId();
        const typingSender = document.getElementById('typing-sender');
        if (convId && typingSender) {
            const subscribeMsg = {
                type: "subscribe",
                conversation_id: convId
            };
            typingSender.setAttribute('hx-vals', JSON.stringify(subscribeMsg));
            htmx.trigger(typingSender, 'typing-event');
        }
        if (wasReconnecting) {
            // The subscribe's ack backfills too; this covers a server that
            // predates the ack. Duplicates are dropped on arrival.
            backfillMessages(convId);
            catchUpSidebar();
        }
        isReconnecting = false;
    });

    // Show the "reconnecting" bar on a close too, not just on wsError — some
    // drops surface only as a close frame (e.g. a server-initiated 1011).
    document.body.addEventListener('htmx:wsClose', function(evt) {
        const code = evt.detail && evt.detail.event && evt.detail.event.code;
        // The server refused the socket because the session is gone (logged
        // out elsewhere, deactivated, or a dev DB reset). Reload so the login
        // redirect kicks in instead of leaving a chat page with no realtime.
        // The page and the socket check the same session, so this can't loop.
        if (code === 1008 && evt.detail.event.reason === 'Not authenticated') {
            window.location.reload();
            return;
        }
        // Clean closes (normal / going-away) and auth rejects don't reconnect,
        // so don't nag the user with the reconnect bar for them.
        liveSocket = null;
        if (code === 1000 || code === 1001 || code === 1008) return;
        if (!isReconnecting) {
            isReconnecting = true;
            if (connectionStatusBar) connectionStatusBar.style.display = 'block';
        }
    });

    // Proactively reconnect when the tab regains focus or the network returns,
    // rather than waiting for TCP to notice a socket died while backgrounded.
    const reconnectIfNeeded = () => {
        if (typeof window.d8WsReconnectNow === 'function') window.d8WsReconnectNow();
    };
    // A socket that still looks OPEN after the laptop wakes is probably dead:
    // check it with a ping on a short fuse instead of waiting for the next one.
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) return;
        reconnectIfNeeded();
        sendPing(WAKE_PONG_TIMEOUT_MS);
    });
    window.addEventListener('online', () => {
        reconnectIfNeeded();
        sendPing(WAKE_PONG_TIMEOUT_MS);
    });

    const messagesContainer = document.getElementById('chat-messages-container');
    const jumpToBottomBtn = document.getElementById('jump-to-bottom-btn');
    const isUserNearBottom = () => {
        if (!messagesContainer) return false;
        return messagesContainer.scrollHeight - messagesContainer.clientHeight - messagesContainer.scrollTop < 150;
    };
    const scrollLastMessageIntoView = () => {
        const bottomAnchor = document.getElementById('message-list-bottom');
        if (bottomAnchor) {
            bottomAnchor.scrollIntoView({
                behavior: "smooth",
                block: "end"
            });
        }
    };
    // Scroll a specific message into view and briefly highlight it.
    const scrollToMessage = (selector) => {
        const targetMessage = document.querySelector(selector);
        if (!targetMessage) return;
        targetMessage.scrollIntoView({ behavior: 'auto', block: 'center' });
        targetMessage.classList.add('mentioned-message');
        setTimeout(() => targetMessage.classList.remove('mentioned-message'), 2000);
    };

    const getJumpSelector = (xhr) => {
        try {
            const header = xhr && xhr.getResponseHeader('HX-Trigger');
            if (!header) return null;
            const parsed = JSON.parse(header);
            return parsed && parsed.jumpToMessage ? parsed.jumpToMessage : null;
        } catch (e) {
            return null;
        }
    };

    // --- Conversation switching ---------------------------------------------
    // Feedback the moment a conversation is picked: the sidebar highlight moves
    // on click (not when the response lands), the composer is kept and gets
    // that conversation's draft, and the pane is scrolled into place before
    // the browser paints it.
    const setActiveSidebarItem = (convId) => {
        document.querySelectorAll('.sidebar-item-link.active').forEach((a) => {
            a.classList.remove('active');
            a.removeAttribute('aria-current');
        });
        const link = convId && document.getElementById(`link-${convId}`);
        const anchor = link && link.closest('.sidebar-item-link');
        if (anchor) {
            anchor.classList.add('active');
            anchor.setAttribute('aria-current', 'page');
        }
    };
    const convIdForLink = (anchor) => {
        const label = anchor && anchor.querySelector('[id^="link-"]');
        return label ? label.id.slice('link-'.length) : null;
    };

    document.body.addEventListener('htmx:beforeRequest', (evt) => {
        const target = evt.detail.target;
        if (!target || target.id !== 'chat-messages-container') return;
        const leaving = activeConversationId();
        const editor = window.mainEditor;
        if (editor && leaving && editor.state.editor && editor.state.editor.isConnected) {
            DraftManager.save(leaving, editor.getText());
            editor.clear();
        }
        const anchor = evt.detail.elt && evt.detail.elt.closest && evt.detail.elt.closest('.sidebar-item-link');
        if (anchor) setActiveSidebarItem(convIdForLink(anchor));
        NewMessagesPill.reset();
    });

    document.body.addEventListener('htmx:afterSwap', (evt) => {
        const target = evt.detail.target;
        if (!target || target.id !== 'chat-messages-container') return;
        const convId = activeConversationId();
        setActiveSidebarItem(convId);
        // Local-day dividers before the first paint (and before measuring).
        DateDividers.rebuild(document.getElementById('message-list'));
        // Scroll now, in the same task as the swap: waiting for settle (and a
        // timer after it) painted a frame at the wrong offset, then jumped.
        const jumpSelector = getJumpSelector(evt.detail.xhr);
        if (jumpSelector) scrollToMessage(jumpSelector);
        else positionPane();
        StickToBottom.watch();
        // A composer that was replaced restores its draft when it initializes.
        const editor = window.mainEditor;
        if (editor && editor.state.editor && editor.state.editor.isConnected && convId
            && !editor.getText().trim()) {
            const draft = DraftManager.get(convId);
            if (draft) editor.setText(draft);
        }
        ComposerPlaceholder.update();
        TitleManager.update();
    });

    // A pane opens at its "new messages" line, or at the bottom.
    const positionPane = () => {
        const container = document.getElementById('chat-messages-container');
        if (!container) return;
        const separator = container.querySelector('.new-message-separator');
        if (separator) {
            separator.scrollIntoView({ behavior: 'auto', block: 'center' });
        } else {
            container.scrollTop = container.scrollHeight;
        }
    };

    // Keep the view pinned to the newest message while content above it
    // grows (images decoding, code blocks collapsing), if the user was at the
    // bottom. Replaces a load listener per image.
    const StickToBottom = {
        pinned: true,
        observer: null,
        watch() {
            const container = document.getElementById('chat-messages-container');
            const list = document.getElementById('message-list');
            if (!container || !list || typeof ResizeObserver === 'undefined') return;
            if (this.observer) this.observer.disconnect();
            this.pinned = !container.querySelector('.new-message-separator');
            this.observer = new ResizeObserver(() => {
                if (this.pinned) container.scrollTop = container.scrollHeight;
            });
            this.observer.observe(list);
        },
    };
    if (messagesContainer) {
        messagesContainer.addEventListener('scroll', () => {
            StickToBottom.pinned = isUserNearBottom();
        }, { passive: true });
    }

    // "N new messages ↓" while scrolled up; clears on reaching the bottom.
    const NewMessagesPill = {
        count: 0,
        render() {
            if (!jumpToBottomBtn) return;
            if (this.count <= 0) {
                jumpToBottomBtn.style.display = 'none';
                return;
            }
            const label = jumpToBottomBtn.querySelector('.jump-label');
            const text = `${this.count} new ${this.count === 1 ? 'message' : 'messages'}`;
            if (label) label.textContent = text;
            jumpToBottomBtn.style.display = 'block';
        },
        add(n = 1) {
            this.count += n;
            this.render();
        },
        reset() {
            this.count = 0;
            this.render();
        },
    };

    // --- Click a quoted reply → go to the original message ---
    document.body.addEventListener('click', (e) => {
        const quote = e.target.closest('.quoted-reply-clickable');
        if (!quote) return;
        const targetId = quote.dataset.targetMessageId;
        if (!targetId) return;
        const selector = '#message-' +
            ((window.CSS && CSS.escape) ? CSS.escape(targetId) : targetId);

        if (document.querySelector(selector)) {
            scrollToMessage(selector);
        } else if (quote.dataset.jumpUrl && window.htmx) {
            window.htmx.ajax('GET', quote.dataset.jumpUrl, {
                target: '#chat-messages-container',
                swap: 'innerHTML',
            });
        }
    });
    const initializeTooltips = (container) => {
        const tooltipTriggerList = container.querySelectorAll('[data-bs-toggle="tooltip"]');
        [...tooltipTriggerList].map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl));
    };
    const updateReactionHighlights = (container) => {
        const currentUserId = document.querySelector('main.main-content').dataset.currentUserId;
        if (!currentUserId) return;
        const reactionPills = container.querySelectorAll('.reaction-pill');
        reactionPills.forEach(pill => {
            const reactorIds = pill.dataset.reactorIds || '';
            const hasReacted = reactorIds.split(',').includes(currentUserId);
            pill.classList.toggle('user-reacted', hasReacted);
        });
    };
    const initializeReactionPopovers = (container) => {
        const popoverTriggerList = container.querySelectorAll('[data-bs-toggle="popover"]');
        popoverTriggerList.forEach(popoverTriggerEl => {
            if (popoverTriggerEl.dataset.popoverInitialized) return;
            const messageId = popoverTriggerEl.dataset.messageId;
            if (!messageId) return;
            const popoverOptions = { html: true, sanitize: false, content: `<emoji-picker class="light" data-source="/js/local/emoji.json"></emoji-picker>`, placement: 'top', customClass: 'emoji-popover' };
            if (popoverTriggerEl.closest('#right-panel-offcanvas')) {
                popoverOptions.container = '#right-panel-offcanvas';
            }
            const popover = new bootstrap.Popover(popoverTriggerEl, popoverOptions);
            popoverTriggerEl.addEventListener('shown.bs.popover', () => {
                const picker = document.querySelector(`.popover[role="tooltip"] emoji-picker`);
                if (picker) {
                    const pickerListener = event => {
                        htmx.ajax('POST', `/chat/message/${messageId}/react`, {
                            values: { emoji: event.detail.unicode }, swap: 'none'
                        });
                        popover.hide();
                    };
                    picker.removeEventListener('emoji-click', pickerListener);
                    picker.addEventListener('emoji-click', pickerListener, { once: true });
                }
            });
            popoverTriggerEl.dataset.popoverInitialized = 'true';
        });
    };
    const processCodeBlocks = (container) => {
        const MAX_HEIGHT = 300;
        setTimeout(() => {
            const codeBlocks = container.querySelectorAll('.codehilite:not(.code-processed)');
            codeBlocks.forEach(block => {
                // If scrollHeight is greater than MAX_HEIGHT, it's a tall block hiding content
                if (block.scrollHeight > MAX_HEIGHT) {
                    const toggler = document.createElement('div');
                    toggler.className = 'code-expander';
                    toggler.innerText = 'Show more...';
                    block.appendChild(toggler);
                    toggler.addEventListener('click', () => {
                        block.classList.toggle('expanded');
                        toggler.innerText = block.classList.contains('expanded') ? 'Show less' : 'Show more...';
                    });
                } else {
                    // It's a short block, remove the 300px constraint so it flexes naturally
                    block.classList.add('short');
                }
                block.classList.add('code-processed');
            });
        }, 50);
    };
    const formatLocalTimes = (container) => {
        const timeElements = container.querySelectorAll('.local-time:not(.time-processed)');
        timeElements.forEach(el => {
            const timestamp = el.dataset.timestamp;
            if (timestamp) {
                const date = new Date(timestamp);
                // Format to match our python output "%I:%M %p" (e.g., "02:42 PM") using the user's browser timezone
                el.textContent = date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true }).toUpperCase();
            }
            el.classList.add('time-processed');
        });
    };
    const handleMentionHighlights = (container, conversationId) => {
        const mentions = container.querySelectorAll('.mentioned-message');
        if (mentions.length === 0) return;
        let latestMentionId = 0;
        mentions.forEach(mentionEl => {
            const messageId = parseInt(mentionEl.id.split('-')[1]);
            if (messageId > latestMentionId) { latestMentionId = messageId; }
            setTimeout(() => { mentionEl.classList.remove('mentioned-message'); }, 3000);
        });
        if (latestMentionId > 0 && conversationId) {
            const updateUrl = `/chat/conversation/${conversationId}/seen_mentions`;
            const triggerEl = document.createElement('div');
            triggerEl.setAttribute('hx-post', updateUrl);
            triggerEl.setAttribute('hx-vals', `{"last_message_id": ${latestMentionId}}`);
            triggerEl.setAttribute('hx-trigger', 'load');
            document.body.appendChild(triggerEl);
            htmx.process(triggerEl);
            setTimeout(() => document.body.removeChild(triggerEl), 100);
        }
    };
    document.body.addEventListener('focus-chat-input', () => {
        if (window.mainEditor && typeof window.mainEditor.focusActiveInput === 'function') {
            window.mainEditor.focusActiveInput();
        }
    });
    // Preference changes arrive as HX-Trigger events. htmx passes a plain
    // (non-object) trigger value to listeners as evt.detail.value.
    document.body.addEventListener('update-sound-preference', (evt) => {
        const newSound = evt.detail.value;
        if (newSound && NotificationManager) {
            console.log(`Updating notification sound to: ${newSound}`);
            NotificationManager.soundFile = newSound;
        }
    });
    // Preferences: send-key choice changed. Update the DOM flag the keydown
    // handler reads (so it applies immediately, no reload) and refresh the
    // visible send-button hints to match.
    document.body.addEventListener('update-send-on-enter', (evt) => {
        const enabled = evt.detail.value !== false && evt.detail.value !== 'false';
        const main = document.querySelector('main.main-content');
        if (main) main.dataset.sendOnEnter = enabled ? 'true' : 'false';
        document.querySelectorAll('[id^="send-button"]').forEach((btn) => {
            const form = btn.closest('form');
            const replyInput = form && form.querySelector('input[name="reply_type"]');
            const label = btn.dataset.sendLabel
                || (replyInput && replyInput.value === 'quote' ? 'Reply' : 'Send');
            if (enabled) {
                btn.innerHTML = `<span>${label}</span><span class="send-shortcut"><i class="bi bi-arrow-return-left"></i></span>`;
                btn.title = `${label} (Enter)`;
            } else {
                btn.innerHTML = `<span>${label}</span><span class="send-shortcut"><kbd>Ctrl</kbd>+<i class="bi bi-arrow-return-left"></i></span>`;
                btn.title = `${label} (Ctrl+Enter)`;
            }
        });
    });
    document.addEventListener('click', (e) => {
        if (e.target.closest('[id^="emoji-btn"]')) { return; }
        const openPicker = document.querySelector('[id^="emoji-picker-container"][style*="block"]');
        if (openPicker && openPicker.contains(e.target)) { return; }
        document.querySelectorAll('[id^="emoji-picker-container"]').forEach(picker => {
            picker.style.display = 'none';
        });
    });
    document.addEventListener('click', (e) => {
        // Dismiss any open ":shortcode" autocomplete popover on an outside click.
        document.querySelectorAll('.emoji-suggestion-popover').forEach(popover => {
            if (!popover.contains(e.target) && typeof popover.emojiHide === 'function') {
                popover.emojiHide();
            }
        });
    });
    const updateTypingIndicator = (typists = []) => {
        const indicators = document.querySelectorAll('[id^="typing-indicator"]');
        if (!indicators.length) return;
        const currentUsername = document.querySelector('main.main-content')?.dataset.currentUserUsername;
        const otherTypists = typists.filter(username => username !== currentUsername);
        let message = '';
        const count = otherTypists.length;
        if (count === 1) {
            message = `${otherTypists[0]} is typing...`;
        } else if (count === 2) {
            message = `${otherTypists[0]} and ${otherTypists[1]} are typing...`;
        } else if (count > 2) {
            message = 'Several people are typing...';
        }
        // Only touch the DOM when the text actually changes — typing_update
        // events arrive on every keystroke, and rewriting identical text just
        // causes needless repaints/flicker.
        indicators.forEach(indicator => {
            if (indicator.textContent !== message) indicator.textContent = message;
        });
    };
    // Measured before a frame's content lands, so its own height doesn't
    // count against "was the user at the bottom?".
    document.body.addEventListener('htmx:wsBeforeMessage', function() {
        if (activeConversationId()) StickToBottom.pinned = isUserNearBottom();
    });

    document.body.addEventListener('htmx:afterSettle', (event) => {
        const target = event.detail.target;

        // (Positioning a freshly opened pane happens in htmx:afterSwap, before
        // the first paint.)

        // A pane opened or a page of history added: local-day dividers.
        if (target.id === 'chat-messages-container' || target.closest && target.closest('#message-list')
            || (target.classList && (target.classList.contains('older-message-loader') || target.classList.contains('newer-message-loader')))) {
            DateDividers.rebuild(document.getElementById('message-list'));
        }

        // Re-initialize dynamic components on the newly swapped content.
        // This prevents things like tooltips and popovers from breaking after a swap.
        if (target) {
            initializeTooltips(target);
            updateReactionHighlights(target);
            initializeReactionPopovers(target);
            processCodeBlocks(target);
            formatLocalTimes(target);

            // If the swapped content is the main chat container,
            // also handle highlighting any new mentions.
            if (target.id === 'chat-messages-container') {
                const conversationDiv = target.querySelector('[data-conversation-db-id]');
                if (conversationDiv) {
                    const conversationId = conversationDiv.dataset.conversationDbId;
                    handleMentionHighlights(target, conversationId);
                }
            }
        }
    });

    // The DM list is most-recent first, under the user's own row, like the
    // server's initial render.
    function moveDmRowToTop(row) {
        if (!row || !row.parentNode) return;
        const selfRow = document.getElementById('dm-self-item');
        if (selfRow && selfRow.parentNode === row.parentNode) {
            if (selfRow.nextElementSibling !== row) selfRow.after(row);
        } else if (row.parentNode.firstElementChild !== row) {
            row.parentNode.prepend(row);
        }
    }

    document.body.addEventListener('htmx:oobBeforeSwap', function(evt) {
        const frag = evt.detail.fragment;
        if (!frag || !frag.querySelector) return;

        const target = evt.detail.target;

        // Opening a conversation sends a fresh default composer. Keep the one
        // on screen when it's already the default: replacing it rebuilt the
        // editor and its emoji picker on every switch and dropped focus. (Its
        // draft is swapped per conversation in htmx:beforeRequest/afterSwap.)
        if (target && target.id === 'chat-input-container') {
            const isDefault = (form) => !!form && form.hasAttribute('hx-post')
                && !form.querySelector('input[name="reply_type"]');
            if (isDefault(frag.querySelector('#message-form')) && isDefault(target.querySelector('#message-form'))) {
                evt.preventDefault();
            }
            return;
        }

        // An append tagged for another conversation than the one on screen:
        // the socket was still subscribed to the previous pane, or a backfill
        // came back after the user switched. Drop it.
        if (target && target.id === 'message-list' && frag.getAttribute) {
            const fragConv = frag.getAttribute('data-conversation-id');
            const pane = target.closest('[data-conversation-id]');
            if (fragConv && pane && pane.dataset.conversationId !== fragConv) {
                evt.preventDefault();
                return;
            }
        }

        // DM notifications and the reconnect sidebar catch-up ride a sidebar
        // row along with the unread badge, so a client whose sidebar predates
        // the conversation can materialize it. When the row is already there,
        // move it to the top instead of appending a second one; a new row is
        // moved up once it lands (htmx:oobAfterSwap).
        const row = frag.matches && frag.matches('li[id^="dm-item-"]')
            ? frag
            : frag.querySelector('li[id^="dm-item-"]');
        if (!row) return;
        const existing = document.getElementById(row.id);
        if (existing) {
            moveDmRowToTop(existing);
            evt.preventDefault();
        } else {
            evt.detail.d8NewDmRowId = row.id;
        }
    });

    document.body.addEventListener('htmx:oobAfterSwap', function(evt) {
        const target = evt.detail.target; // The element that was swapped.

        if (evt.detail.d8NewDmRowId) {
            moveDmRowToTop(document.getElementById(evt.detail.d8NewDmRowId));
        }
        // A message just landed in the open DM (sent or received): it is now
        // the most recent conversation.
        if (target && target.id === 'message-list') {
            const pane = target.closest('[data-conversation-id]');
            const convId = pane && pane.dataset.conversationId;
            if (convId && convId.startsWith('dm_')) {
                moveDmRowToTop(document.getElementById(`dm-item-${convId}`));
            }
        }

        // --- Re-initialize components for edited messages received via WebSocket ---
        if (target && target.classList && target.classList.contains('message-container')) {
            // An edit is an outerHTML swap, and htmx reports the replaced node,
            // already detached. Work on its live replacement, or the edited
            // message keeps its UTC time and loses its tooltips/highlights.
            const live = target.isConnected ? target : document.getElementById(target.id);
            if (live) {
                initializeTooltips(live);
                updateReactionHighlights(live);
                initializeReactionPopovers(live);
                processCodeBlocks(live);
                formatLocalTimes(live);
            }
        }

        // --- Reaction updates ---
        // The server renders a reaction change once, for the person who
        // reacted, so its `user-reacted` highlights are theirs. Recompute them
        // for this viewer.
        if (target && target.id && target.id.startsWith('reactions-container-')) {
            updateReactionHighlights(target);
        }

        // --- Scrolling logic for new messages in a thread ---
        if (target && target.id.startsWith('thread-replies-list-')) {
            // Localize the appended reply's timestamp (see note in the
            // message-list branch); otherwise live thread replies show UTC.
            formatLocalTimes(target);
            target.querySelectorAll('.message-container[data-client-id]:not(.pending-message)').forEach((el) => {
                PendingMessages.resolve(el.dataset.clientId);
            });
            PendingMessages.keepAtBottom(target);

            const scrollableContainer = target.closest('.flex-grow-1[style*="overflow-y: auto"]');
            if (scrollableContainer) {
                const isNearBottom = scrollableContainer.scrollHeight - scrollableContainer.clientHeight - scrollableContainer.scrollTop < 150;
                if (isNearBottom) {
                    const scrollThread = () => {
                        scrollableContainer.scrollTo({
                            top: scrollableContainer.scrollHeight,
                            behavior: 'smooth'
                        });
                    };
                    scrollThread();

                    // NEW: Wait for images to load, then scroll again
                    if (target.lastElementChild) {
                        const images = target.lastElementChild.querySelectorAll('img:not(.avatar-image)');
                        images.forEach(img => {
                            img.addEventListener('load', scrollThread);
                        });
                    }
                }
            }
        }

        // --- Scrolling logic for new messages in the main chat view ---
        if (target && target.id === 'message-list') {
            // Dedupe: drop any message a live broadcast and a reconnect catch-up
            // both delivered, keeping the earliest copy already in the DOM.
            const seenIds = new Set();
            target.querySelectorAll('.message-container[id^="message-"]').forEach((el) => {
                if (seenIds.has(el.id)) el.remove();
                else seenIds.add(el.id);
            });
            // Same for date dividers: the server sends one with the first
            // message of a new day, so a message delivered twice brings its
            // divider twice. The duplicate can land after the first copy of the
            // message, not next to the original divider, so drop any divider
            // that repeats the label of the divider before it. Dividers are in
            // date order, so a repeated label is always the same day.
            let lastDividerLabel = null;
            target.querySelectorAll('.date-separator').forEach((el) => {
                const label = el.textContent.trim();
                if (label === lastDividerLabel) el.remove();
                else lastDividerLabel = label;
            });
            // ...then redraw them from local days (see DateDividers).
            DateDividers.rebuild(target);

            // Localize timestamps on every freshly appended message. The server
            // renders times in UTC as a fallback; without this, live/broadcast
            // and catch-up messages stay in UTC until a full page reload. Runs on
            // the whole list (idempotent via the .time-processed guard) so a
            // multi-message catch-up backfill localizes all of them, not just the
            // last one.
            formatLocalTimes(target);

            // A real message replaces its optimistic copy; copies still in
            // flight stay below everything that arrived meanwhile.
            target.querySelectorAll('.message-container[data-client-id]:not(.pending-message)').forEach((el) => {
                PendingMessages.resolve(el.dataset.clientId);
            });
            PendingMessages.keepAtBottom(target);

            const messagesContainer = document.getElementById('chat-messages-container');
            const mainContent = document.querySelector('main.main-content');
            const currentUserId = mainContent.dataset.currentUserId;
            const arrived = target.querySelectorAll('.message-container:not(.pending-message), .system-message');
            const lastMessage = arrived[arrived.length - 1];

            if (lastMessage && messagesContainer && !lastMessage.dataset.d8Seen) {
                lastMessage.dataset.d8Seen = '1';
                const mine = lastMessage.dataset.userId === currentUserId;
                if (mine || StickToBottom.pinned) {
                    scrollLastMessageIntoView();
                } else {
                    NewMessagesPill.add();
                }
                lastMessage.classList.add('just-arrived');
                setTimeout(() => lastMessage.classList.remove('just-arrived'), 400);

                // Since a new message was added, initialize its dynamic elements.
                if (lastMessage.matches('.message-container')) {
                    initializeReactionPopovers(lastMessage);
                    processCodeBlocks(lastMessage);
                    updateReactionHighlights(lastMessage); // Ensure reactions are highlighted correctly
                }
            }
        }

        // After any OOB swap, it's possible the unread count changed.
        FaviconManager.updateFavicon();
    });

    document.body.addEventListener('htmx:wsAfterMessage', (event) => {
        // 1. First, try to parse the message as JSON for special events.
        try {
            const data = JSON.parse(event.detail.message);
            if (typeof data === 'object' && data.type) {
                if (data.type === 'typing_update') { updateTypingIndicator(data.typists); return; }

                if (data.type === 'pong') return;

                // The socket now follows this conversation: fetch whatever was
                // posted between the pane's render and the subscribe.
                if (data.type === 'subscribed') {
                    if (data.conversation_id === activeConversationId()) {
                        backfillMessages(data.conversation_id);
                    }
                    return;
                }

                if (data.type === 'presence_update') {
                    const { user_id, status_class } = data;

                    // Find all possible presence indicators for this user on the page
                    const indicators = document.querySelectorAll(
                        `#status-dot-${user_id}, #sidebar-presence-indicator-${user_id}, #profile-presence-indicator-${user_id}`
                    );

                    indicators.forEach(indicator => {
                        // Replace the old status class with the new one
                        indicator.className = indicator.className.replace(/presence-(online|away|busy)/, status_class);
                    });
                    return; // End processing for this event
                }

                if (data.type === 'avatar_update') {
                    const { user_id, avatar_url } = data;
                    const avatarImages = document.querySelectorAll(`.avatar-image[data-user-id="${user_id}"]`);
                    avatarImages.forEach(el => {
                        if (el.tagName === 'IMG') {
                            el.src = avatar_url;
                        } else {
                            const newImg = document.createElement('img');
                            newImg.src = avatar_url;
                            newImg.alt = `${el.textContent.trim()}'s avatar`;
                            newImg.className = 'rounded-circle avatar-image';
                            newImg.style.cssText = el.style.cssText;
                            newImg.style.objectFit = 'cover';
                            newImg.dataset.userId = user_id;
                            el.replaceWith(newImg);
                        }
                    });
                    return;
                }
                // On-call pages ring regardless of focus or the open channel (pager.js).
                if (data.type === 'page' || data.type === 'page_cleared') {
                    if (window.PagerManager) window.PagerManager.handleFrame(data);
                    return;
                }
                if (data.type === 'notification') NotificationManager.showNotification(data);
                else if (data.type === 'sound') NotificationManager.playSound();
                return; // End processing for JSON events.
            }
        } catch (e) {
            // Not a JSON message, so it's HTML. Proceed below.
        }

        // 2. Otherwise it's HTML: every frame the server sends is OOB swaps,
        // which htmx applies itself. The unread count may have changed.
        FaviconManager.updateFavicon();
    });

    if (messagesContainer) {
        messagesContainer.addEventListener('scroll', () => {
            if (isUserNearBottom()) NewMessagesPill.reset();
        }, { passive: true });
    }
    if (jumpToBottomBtn) {
        jumpToBottomBtn.addEventListener('click', () => {
            scrollLastMessageIntoView();
            NewMessagesPill.reset();
        });
    }
    const htmxModalEl = document.getElementById('htmx-modal');
    if (htmxModalEl) {
        const htmxModal = new bootstrap.Modal(htmxModalEl);
        document.body.addEventListener('close-modal', () => htmxModal.hide());
        htmxModalEl.addEventListener('shown.bs.modal', () => {
            const autofocusEl = htmxModalEl.querySelector('[autofocus]');
            if (autofocusEl) { autofocusEl.focus(); }
        });
        htmxModalEl.addEventListener('hidden.bs.modal', () => {
            if (window.focusChatInputAfterModalClose) {
                if (window.mainEditor && typeof window.mainEditor.focusActiveInput === 'function') {
                    window.mainEditor.focusActiveInput();
                }
                window.focusChatInputAfterModalClose = false;
            }
            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) modalContent.innerHTML = `<div class="modal-body text-center"><div class="spinner-border" role="status"></div></div>`;
        });
    }
    const rightPanelOffcanvasEl = document.getElementById('right-panel-offcanvas');
    if (rightPanelOffcanvasEl) {
        const rightPanelOffcanvas = new bootstrap.Offcanvas(rightPanelOffcanvasEl);
        document.body.addEventListener('close-offcanvas', () => rightPanelOffcanvas.hide());
        document.body.addEventListener('open-offcanvas', () => rightPanelOffcanvas.show());
        rightPanelOffcanvasEl.addEventListener('shown.bs.offcanvas', event => {
            const threadRepliesList = rightPanelOffcanvasEl.querySelector('[id^="thread-replies-list-"]');
            if (threadRepliesList) {
                const scrollableContainer = threadRepliesList.closest('.flex-grow-1[style*="overflow-y: auto"]');
                if (scrollableContainer) {
                    scrollableContainer.scrollTop = scrollableContainer.scrollHeight;
                }
            }
        });
        // Thread and profile views replace the whole panel (including the
        // #right-panel-body this used to reset), so the next open showed the
        // previous thread until its own arrived. Reset the panel itself, on
        // close and whenever new content is requested for it.
        const resetRightPanel = () => {
            rightPanelOffcanvasEl.innerHTML = `<div class="offcanvas-body" id="right-panel-body"><div class="text-center"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div></div>`;
        };
        rightPanelOffcanvasEl.addEventListener('hidden.bs.offcanvas', resetRightPanel);
        document.body.addEventListener('htmx:beforeRequest', (evt) => {
            if (evt.detail.target === rightPanelOffcanvasEl) resetRightPanel();
        });
    }
    document.body.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (searchOverlay && searchOverlay.style.display !== 'none') {
                e.preventDefault();
                const closeBtn = document.getElementById('close-search-btn');
                if (closeBtn) closeBtn.click();
                return;
            }
            const visiblePopover = document.querySelector('.popover.show');
            if (visiblePopover) {
                e.preventDefault();
                const triggerEl = document.querySelector(`[aria-describedby="${visiblePopover.id}"]`);
                if (triggerEl) {
                    const popoverInstance = bootstrap.Popover.getInstance(triggerEl);
                    if (popoverInstance) { popoverInstance.hide(); }
                }
                return;
            }
            if (rightPanelOffcanvasEl && rightPanelOffcanvasEl.classList.contains('show')) {
                e.preventDefault();
                const offcanvasInstance = bootstrap.Offcanvas.getInstance(rightPanelOffcanvasEl);
                if (offcanvasInstance) offcanvasInstance.hide();
                return;
            }
            if (htmxModalEl && htmxModalEl.classList.contains('show')) {
                e.preventDefault();
                const modalInstance = bootstrap.Modal.getInstance(htmxModalEl);
                if (modalInstance) modalInstance.hide();
                return;
            }
            const openPicker = document.querySelector('[id^="emoji-picker-container"][style*="block"]');
            if (openPicker) {
                e.preventDefault();
                openPicker.style.display = 'none';
                return;
            }
            // Leave an edit or quote-reply, back to the plain composer.
            const cancel = document.querySelector('#chat-input-container .btn-close[hx-get="/chat/input/default"]');
            if (cancel) {
                e.preventDefault();
                cancel.click();
                return;
            }
        }
    });
    DraftManager.markAll();
    processCodeBlocks(document.body);
    initializeReactionPopovers(document.body);
    updateReactionHighlights(document.body);
    initializeTooltips(document.body);
    formatLocalTimes(document.body);

    // --- Presence: idle/away detection -------------------------------------
    // A live WebSocket only tells us a tab is open, not that someone is at the
    // keyboard — so a tab left open overnight would show green all morning.
    // Here the browser reports real activity: we stamp "active" while the user
    // interacts and "away" after IDLE_MS of no input, and the server maps that
    // to the online/away dot. A manually-chosen busy/away is honored as-is and
    // never auto-flipped.
    (function initPresence() {
        const main = document.querySelector('main.main-content');
        const activityUrl = main && main.dataset.activityUrl;
        if (!main || !activityUrl) return;

        const IDLE_MS = 15 * 60 * 1000;      // idle before we report "away"
        const KEEPALIVE_MS = 4 * 60 * 1000;  // refresh "active" while present
        const CHECK_MS = 30 * 1000;          // how often we re-evaluate

        // The status the user explicitly chose. Auto-away only manages the
        // 'online' case; 'busy'/'away' are left exactly as the user set them.
        let manualStatus = main.dataset.currentUserStatus || 'online';
        let lastActivity = Date.now();
        let lastPing = 0;
        let autoAway = false;

        const send = (state) => {
            // keepalive lets a final ping survive a tab closing/navigating away.
            // This fetch bypasses htmx's configRequest hook, so the CSRF token
            // has to be added by hand; without it every ping was a 400.
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
            fetch(activityUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    ...(csrfToken ? { 'X-CSRFToken': csrfToken } : {}),
                },
                body: new URLSearchParams({ state }),
                keepalive: true,
            }).catch(() => {});
            lastPing = Date.now();
        };

        const onActivity = () => {
            lastActivity = Date.now();
            if (autoAway && manualStatus === 'online') {
                autoAway = false;
                send('active'); // returned from idle -> back online
            }
        };

        ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'wheel'].forEach((evt) => {
            document.addEventListener(evt, onActivity, { passive: true });
        });

        // Follow manual status choices from the profile menu so auto-away never
        // fights a deliberate away/busy. Delegated so it survives OOB re-renders.
        document.body.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-presence-choice]');
            if (!btn) return;
            manualStatus = btn.dataset.presenceChoice;
            autoAway = false;
            lastActivity = Date.now();
        });

        setInterval(() => {
            if (manualStatus !== 'online') return; // busy/away: leave untouched
            if (Date.now() - lastActivity >= IDLE_MS) {
                if (!autoAway) {
                    autoAway = true;
                    send('away');
                }
            } else if (Date.now() - lastPing >= KEEPALIVE_MS && lastActivity > lastPing) {
                // Re-stamp only when there was real input since the last ping:
                // the stamp's age is what the server's push gate reads as
                // "actively using the web app", so an untouched tab must let
                // its score go stale (mobile pushes resume) even before the
                // 15-minute away flip.
                send('active');
            }
        }, CHECK_MS);

        // Stamp presence on load: present users (online/busy) go into the active
        // set immediately; a manually-away user is left as-is (no ping).
        if (manualStatus !== 'away') send('active');
    })();
});
