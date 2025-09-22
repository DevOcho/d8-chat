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
        const button = document.getElementById('enable-notifications-btn');
        if (!button) return;
        if (Notification.permission === "default") {
            button.style.display = 'block';
            button.addEventListener('click', this.requestPermission.bind(this));
        }
    },
    requestPermission: function() {
        Notification.requestPermission().then(permission => {
            const button = document.getElementById('enable-notifications-btn');
            if (button) button.style.display = 'none';
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
        if (Notification.permission === "granted") {
            this.playSound();
            const notification = new Notification(data.title, {
                body: data.body,
                icon: data.icon,
                tag: data.tag
            });
            notification.onclick = () => { window.focus(); };
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
        thumbnailDiv.innerHTML = `<img src="" alt="Uploading..." /><div class="spinner-border spinner-border-sm text-light position-absolute top-50 start-50"></div><button type="button" class="remove-attachment-btn">&times;</button>`;
        state.previewContainer.appendChild(thumbnailDiv);
        const reader = new FileReader();
        reader.onload = (e) => { thumbnailDiv.querySelector('img').src = e.target.result; };
        reader.readAsDataURL(file);
        const formData = new FormData();
        formData.append('file', file);
        fetch('/files/upload', { method: 'POST', body: formData })
            .then(response => {
                thumbnailDiv.querySelector('.spinner-border').remove();
                if (!response.ok) return response.json().then(err => { throw err; });
                return response.json();
            })
            .then(data => {
                if (data && data.file_id) {
                    const upload = state.uploads.get(uploadKey);
                    upload.fileId = data.file_id;
                    upload.status = 'success';
                    updateHiddenInput();
                }
            })
            .catch(error => {
                const errorMessage = error.error || "Upload failed";
                console.error("Upload failed for key", uploadKey, ":", errorMessage);
                ToastManager.show('Upload Error', errorMessage, 'danger');
                const upload = state.uploads.get(uploadKey);
                if (upload) upload.status = 'error';
                thumbnailDiv.style.opacity = '0.5';
                thumbnailDiv.title = errorMessage;
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
            if (item) selectItem(item);
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
            text = editor.textContent;
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
            e.stopPropagation();
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
            let textNode = range.startContainer;
            range.setStart(textNode, state.triggerPosition);
            range.setEnd(textNode, range.startOffset);
            range.deleteContents();
            document.execCommand('insertText', false, `@${username} `);
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

/**
 * Factory function to create a self-contained editor instance.
 */
const createEditor = function(idSuffix = '') {
    const state = {};
    let attachmentManager = null;
    let mentionManager = null;

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
            isMarkdownMode,
            messageForm
        } = state;
        const replyTypeInput = messageForm.querySelector('input[name="reply_type"]');
        const isQuoteReply = replyTypeInput && replyTypeInput.value === 'quote';
        const sendText = isQuoteReply ? "Reply" : "Send";
        if (isMarkdownMode) {
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
        const markdownContent = turndownService.turndown(htmlContent).trim();
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
    const setupInputListeners = function() {
        state.editor.addEventListener('input', () => {
            updateStateAndButtons();
            resizeActiveInput();
            clearTimeout(state.typingTimer);
            sendTypingStatus(true);
            state.typingTimer = setTimeout(() => sendTypingStatus(false), 1500);
        });
        state.markdownView.addEventListener('input', () => {
            resizeActiveInput();
            clearTimeout(state.typingTimer);
            sendTypingStatus(true);
            state.typingTimer = setTimeout(() => sendTypingStatus(false), 1500);
        });
        document.addEventListener('selectionchange', () => {
            if (document.activeElement === state.editor) {
                updateStateAndButtons();
            }
        });
    };
    const setupKeydownListeners = function() {
        const currentUserId = document.querySelector('main.main-content').dataset.currentUserId;
        const keydownHandler = (e) => {
            if (mentionManager && mentionManager.state.active) {
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
                const lastUserMessage = document.querySelector(`.message-container[data-user-id="${currentUserId}"]:last-of-type`);
                if (lastUserMessage) {
                    const editButton = lastUserMessage.querySelector('.message-toolbar button[data-action="edit"]');
                    if (editButton) htmx.trigger(editButton, 'click');
                }
                return;
            }
            if (e.key === 'Enter') {
                if (state.isMarkdownMode && !e.shiftKey) {
                    e.preventDefault();
                    if (state.markdownView.value.trim() !== '') state.messageForm.requestSubmit();
                } else if (!state.isMarkdownMode && e.ctrlKey) {
                    e.preventDefault();
                    if (state.editor.innerText.trim() !== '') state.messageForm.requestSubmit();
                }
            }
        };
        state.editor.addEventListener('keydown', keydownHandler);
        state.markdownView.addEventListener('keydown', keydownHandler);
    };
    const setupFormListeners = function() {
        state.messageForm.addEventListener('submit', (e) => {
            if (state.isMarkdownMode) {
                const rawMarkdown = state.markdownView.value;
                state.hiddenInput.value = preprocessMarkdown(rawMarkdown);
            } else {
                updateStateAndButtons();
            }
            const hasText = state.hiddenInput.value.trim() !== '';
            const hasAttachment = state.hiddenAttachmentIds ? state.hiddenAttachmentIds.value !== '' : false;
            if (!hasText && !hasAttachment) {
                e.preventDefault();
                return;
            }
            clearTimeout(state.typingTimer);
            sendTypingStatus(false);
        });
        state.messageForm.addEventListener('htmx:wsAfterSend', () => {
            const isThread = idSuffix.startsWith('-thread-');

            if (isThread) {
                const parentMessageId = idSuffix.split('-').pop();
                htmx.ajax('GET', `/chat/input/thread/${parentMessageId}`, {
                    target: `#thread-input-container-${parentMessageId}`,
                    swap: 'outerHTML'
                });
            } else {
                const isQuoteReply = state.messageForm.querySelector('input[name="reply_type"]') && state.messageForm.querySelector('input[name="reply_type"]').value === 'quote';
                if (isQuoteReply) {
                    htmx.ajax('GET', '/chat/input/default', {
                        target: '#chat-input-container',
                        swap: 'outerHTML'
                    });
                } else {
                    const { editor, markdownView, hiddenInput } = state;
                    editor.innerHTML = '';
                    markdownView.value = '';
                    hiddenInput.value = '';
                    if (attachmentManager && typeof attachmentManager.reset === 'function') {
                        attachmentManager.reset();
                    }
                    state.messageForm.setAttribute('ws-send', '');
                    resizeActiveInput();
                    focusActiveInput();
                }
            }
        });
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
        attachmentManager
    };
};


// --- Event Listeners to initialize editors ---
const emojiPickerReady = customElements.whenDefined('emoji-picker');

document.body.addEventListener('initializeEditor', (event) => {
    emojiPickerReady.then(() => {
        const { idSuffix } = event.detail;
        const editorInstance = createEditor(idSuffix);
        editorInstance.initialize();

        // If this is the main chat input, store its manager and set up drag/drop listeners.
        if (idSuffix === '') {
            window.mainAttachmentManager = editorInstance.state.attachmentManager;

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

    const searchInput = document.getElementById('global-search-input');
    const searchOverlay = document.getElementById('search-results-overlay');
    const channelSidebar = document.querySelector('.channel-sidebar');
    const hideSearch = () => {
        if (!searchOverlay || !searchInput) return;
        searchOverlay.style.display = 'none';
        searchInput.value = '';
        htmx.trigger(searchInput, 'htmx:abort');
    };
    if (searchOverlay) {
        searchOverlay.addEventListener('click', (e) => {
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

    document.body.addEventListener('jumpToMessage', (evt) => {
        const selector = evt.detail.value;
        const targetMessage = document.querySelector(selector);
        if (targetMessage) {
            targetMessage.scrollIntoView({ behavior: 'auto', block: 'center' });
            targetMessage.classList.add('mentioned-message');
            setTimeout(() => {
                targetMessage.classList.remove('mentioned-message');
            }, 2000);
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

        // If the server sent back an error with a message, show it in the toast.
        if (evt.detail.xhr.responseText) {
            ToastManager.show('An Error Occurred', evt.detail.xhr.responseText, 'danger');
        }
    });

    document.body.addEventListener('htmx:oobErrorNoTarget', function(evt) {
        const targetSelector = evt.detail.target;
        if (!targetSelector) {
            console.log("Ignoring harmless OOB update for an undefined target.");
            return;
        }
        if (targetSelector.startsWith('#status-dot-') || targetSelector.startsWith('#sidebar-presence-indicator-') || targetSelector.startsWith('#thread-replies-list-') || targetSelector.startsWith('#reactions-container-')) {
            console.log(`Ignoring harmless OOB update for non-visible target: ${targetSelector}`);
            return;
        }
        const errorMessage = `A UI update failed because the target '${targetSelector}' could not be found.`;
        ToastManager.show('UI Sync Error', errorMessage, 'warning');
    });

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
    document.body.addEventListener('htmx:wsOpen', function(evt) {
        console.log("WebSocket Connection Opened.");
        if (isReconnecting && connectionStatusBar) {
            connectionStatusBar.classList.replace('bg-warning', 'bg-success');
            connectionStatusBar.innerHTML = 'Connection restored.';
            setTimeout(() => {
                connectionStatusBar.style.display = 'none';
                connectionStatusBar.classList.replace('bg-success', 'bg-warning');
                connectionStatusBar.innerHTML = `<div class="spinner-border spinner-border-sm me-2"></div> Connection lost. Attempting to reconnect...`;
            }, 2000);
        }
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        const typingSender = document.getElementById('typing-sender');
        if (activeConv && activeConv.dataset.conversationId && typingSender) {
            const subscribeMsg = {
                type: "subscribe",
                conversation_id: activeConv.dataset.conversationId
            };
            typingSender.setAttribute('hx-vals', JSON.stringify(subscribeMsg));
            htmx.trigger(typingSender, 'typing-event');
        }
        isReconnecting = false;
    });

    const messagesContainer = document.getElementById('chat-messages-container');
    const jumpToBottomBtn = document.getElementById('jump-to-bottom-btn');
    const isUserNearBottom = () => {
        if (!messagesContainer) return false;
        return messagesContainer.scrollHeight - messagesContainer.clientHeight - messagesContainer.scrollTop < 150;
    };
    const scrollLastMessageIntoView = () => {
        const lastMessage = document.querySelector('#message-list > .message-container:last-child, #thread-replies-list > .message-container:last-child');
        if (lastMessage) {
            lastMessage.scrollIntoView({
                behavior: "smooth",
                block: "end"
            });
        }
    };
    const scrollToBottomForce = () => {
        if (messagesContainer) {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }
    };
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
            const popoverOptions = { html: true, sanitize: false, content: `<emoji-picker class="light"></emoji-picker>`, placement: 'top', customClass: 'emoji-popover' };
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
        const codeBlocks = container.querySelectorAll('.codehilite:not(.code-processed)');
        codeBlocks.forEach(block => {
            if (block.scrollHeight > MAX_HEIGHT) {
                block.classList.add('collapsible');
                const toggler = document.createElement('div');
                toggler.className = 'code-expander';
                toggler.innerText = 'Show more...';
                block.appendChild(toggler);
                toggler.addEventListener('click', () => {
                    block.classList.toggle('collapsible');
                    toggler.innerText = block.classList.contains('collapsible') ? 'Show more...' : 'Show less';
                });
            }
            block.classList.add('code-processed');
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
    document.body.addEventListener('update-sound-preference', (evt) => {
        const newSound = evt.detail['update-sound-preference'];
        if (newSound && NotificationManager) {
            console.log(`Updating notification sound to: ${newSound}`);
            NotificationManager.soundFile = newSound;
        }
    });
    document.addEventListener('click', (e) => {
        if (e.target.closest('[id^="emoji-btn"]')) { return; }
        const openPicker = document.querySelector('[id^="emoji-picker-container"][style*="block"]');
        if (openPicker && openPicker.contains(e.target)) { return; }
        document.querySelectorAll('[id^="emoji-picker-container"]').forEach(picker => {
            picker.style.display = 'none';
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
        indicators.forEach(indicator => { indicator.textContent = message; });
    };
    let userWasNearBottom = false;
    document.body.addEventListener('htmx:wsBeforeMessage', function() {
        userWasNearBottom = isUserNearBottom();
    });
    document.body.addEventListener('htmx:afterSwap', (event) => {
        const target = event.detail.target;
        if (target.id === 'search-results-overlay') {
            if (!event.detail.xhr.responseText.trim()) {
                hideSearch();
                return;
            }
            searchOverlay.style.display = 'flex';
            const closeSearchBtn = document.getElementById('close-search-btn');
            if (closeSearchBtn) {
                closeSearchBtn.addEventListener('click', hideSearch);
            }
            return;
        }
        processCodeBlocks(target);
        initializeReactionPopovers(target);
        updateReactionHighlights(target);
        initializeTooltips(target);
        if (target.id === 'chat-messages-container' && event.detail.requestConfig.verb === 'get') {
            setTimeout(scrollToBottomForce, 50);
            if (jumpToBottomBtn) jumpToBottomBtn.style.display = 'none';
            if (window.mainEditor) {
                window.mainEditor.focusActiveInput();
            }
            const conversationDiv = target.querySelector('[data-conversation-db-id]');
            if (conversationDiv) {
                const conversationId = conversationDiv.dataset.conversationDbId;
                handleMentionHighlights(target, conversationId);
            }
        }
        FaviconManager.updateFavicon();
    });
    document.body.addEventListener('htmx:oobAfterSwap', function(evt) {
        const targetList = evt.detail.target;
        if (targetList && targetList.id.startsWith('thread-replies-list-')) {
            const scrollableContainer = targetList.closest('.flex-grow-1[style*="overflow-y: auto"]');
            if (scrollableContainer) {
                const isNearBottom = scrollableContainer.scrollHeight - scrollableContainer.clientHeight - scrollableContainer.scrollTop < 150;
                if (isNearBottom) {
                    scrollableContainer.scrollTo({
                        top: scrollableContainer.scrollHeight,
                        behavior: 'smooth'
                    });
                }
            }
        }
    });

    document.body.addEventListener('htmx:wsAfterMessage', (event) => {
        try {
            const data = JSON.parse(event.detail.message);
            if (typeof data === 'object' && data.type) {
                if (data.type === 'typing_update') { updateTypingIndicator(data.typists); return; }
                if (data.type === 'avatar_update') {
                    const { user_id, avatar_url } = data;
                    const avatarImages = document.querySelectorAll(`.avatar-image[data-user-id="${user_id}"]`);
                    avatarImages.forEach(el => {
                        // If it's already an image, just update the source. This is fast.
                        if (el.tagName === 'IMG') {
                            el.src = avatar_url;
                        } else {
                            // If it's the DIV fallback, we must replace it with a new IMG tag.
                            const newImg = document.createElement('img');
                            newImg.src = avatar_url;
                            newImg.alt = `${el.textContent.trim()}'s avatar`;
                            // Set the correct classes for an image element
                            newImg.className = 'rounded-circle avatar-image';
                            // Copy the inline styles to preserve the size (width/height)
                            newImg.style.cssText = el.style.cssText;
                            newImg.style.objectFit = 'cover'; // Add this for consistency with the original img
                            newImg.dataset.userId = user_id; // Re-apply the data-user-id for future updates

                            // Replace the old div with our newly created img element.
                            el.replaceWith(newImg);
                        }
                    });
                    return;
                }
                if (data.type === 'notification') NotificationManager.showNotification(data);
                else if (data.type === 'sound') NotificationManager.playSound();
                return;
            }
        } catch (e) {}
        const messagesContainer = document.getElementById('chat-messages-container');
        const mainContent = document.querySelector('main.main-content');
        const currentUserId = mainContent.dataset.currentUserId;
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage && messagesContainer) {
            const messageAuthorId = lastMessage.dataset.userId;
            if (messageAuthorId === currentUserId) {
                setTimeout(scrollLastMessageIntoView, 0);
            } else {
                if (userWasNearBottom) {
                    setTimeout(scrollLastMessageIntoView, 0);
                } else {
                    if (jumpToBottomBtn) jumpToBottomBtn.style.display = 'block';
                }
            }
            initializeReactionPopovers(messagesContainer);
            processCodeBlocks(messagesContainer);
            const conversationDiv = messagesContainer.querySelector('[data-conversation-db-id]');
            if (conversationDiv) {
                const conversationId = conversationDiv.dataset.conversationDbId;
                handleMentionHighlights(messagesContainer, conversationId);
            }
        }
        FaviconManager.updateFavicon();
    });

    if (messagesContainer) {
        messagesContainer.addEventListener('scroll', () => {
            if (isUserNearBottom() && jumpToBottomBtn) {
                jumpToBottomBtn.style.display = 'none';
            }
        });
    }
    if (jumpToBottomBtn) {
        jumpToBottomBtn.addEventListener('click', () => {
            setTimeout(scrollLastMessageIntoView, 50);
            jumpToBottomBtn.style.display = 'none';
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
        rightPanelOffcanvasEl.addEventListener('hidden.bs.offcanvas', event => {
            const panelBody = rightPanelOffcanvasEl.querySelector('#right-panel-body');
            if (panelBody) {
                panelBody.innerHTML = `<div class="text-center"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div>`;
            }
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
        }
    });
    processCodeBlocks(document.body);
    initializeReactionPopovers(document.body);
    updateReactionHighlights(document.body);
    initializeTooltips(document.body);
});
