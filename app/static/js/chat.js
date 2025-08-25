/**
 * D8-Chat Main JavaScript File
 *
 * This file handles two main responsibilities:
 * 1. The WYSIWYG Chat Input Editor, encapsulated in the `Editor` object.
 * 2. General page-level logic for the chat interface (scrolling, code blocks, modals).
 */

// Object to manage all notification-related logic
const NotificationManager = {
    audio: new Audio('/audio/notification.mp3'), // Pre-load the audio file

    initialize: function() {
        if (!("Notification" in window)) {
            console.log("This browser does not support desktop notification");
            return;
        }

        const button = document.getElementById('enable-notifications-btn');
        if (!button) return;

        if (Notification.permission === "default") {
            button.style.display = 'block';
            button.addEventListener('click', this.requestPermission);
        }
    },

    requestPermission: function() {
        Notification.requestPermission().then(permission => {
            const button = document.getElementById('enable-notifications-btn');
            if (button) button.style.display = 'none';
        });
    },

    playSound: function() {
        this.audio.play().catch(error => {
            // Autoplay was prevented. This can happen if the user hasn't interacted
            // with the page yet. It's often not a critical error to show.
            console.log("Audio play failed:", error);
        });
    },

    showNotification: function(data) {
        if (Notification.permission === "granted") {
            // Play the sound when showing the notification
            this.playSound();

            const notification = new Notification(data.title, {
                body: data.body,
                icon: data.icon,
                tag: data.tag
            });
            notification.onclick = () => {
                window.focus();
            };
        }
    }
};

const AttachmentManager = {
    state: {},

    initialize: function(editorState) {
        this.state = {
            editorState: editorState,
            fileInput: document.getElementById('file-attachment-input'),
            attachmentBtn: document.getElementById('file-attachment-btn'),
            hiddenAttachmentId: document.getElementById('attachment-file-id'),
            uploadInProgress: false
        };
        this.bindEvents();
    },

    bindEvents: function() {
        if (!this.state.fileInput || !this.state.attachmentBtn) return;

        this.state.attachmentBtn.addEventListener('click', () => {
            this.state.fileInput.click(); // Trigger the hidden file input
        });

        this.state.fileInput.addEventListener('change', this.handleFileSelect.bind(this));
    },

    handleFileSelect: function(e) {
        const file = e.target.files[0];
        if (!file || this.state.uploadInProgress) return;

        this.state.uploadInProgress = true;
        console.log("Uploading file:", file.name);

        const formData = new FormData();
        formData.append('file', file);

        // Use the browser's native `fetch` API for file upload.
        fetch('/files/upload', {
            method: 'POST',
            body: formData,
        })
        .then(response => {
            if (!response.ok) {
                // If the server returns an error (like 400 or 500), throw an error
                // to be caught by the .catch() block.
                return response.json().then(err => { throw err; });
            }
            return response.json(); // If the response is OK, parse the JSON
        })
        .then(data => {
            if (data && data.file_id) {
                console.log("Upload successful. File ID:", data.file_id);
                this.state.hiddenAttachmentId.value = data.file_id;

                // [THE FIX] Manually construct the JSON payload for ws-send
                const messageForm = this.state.editorState.messageForm;
                const payload = {
                    // Include the text content from the hidden input
                    chat_message: this.state.editorState.hiddenInput.value,
                    // And add our new attachment ID
                    attachment_file_id: data.file_id
                };

                // Set the attribute that the htmx-ws extension will read from
                messageForm.setAttribute('ws-send', JSON.stringify(payload));

                // Now, trigger the form submission. htmx-ws will use our new attribute.
                messageForm.requestSubmit();
            }
        })
        .catch(error => {
            const errorMessage = error.error || "An unknown upload error occurred.";
            console.error("Upload failed:", errorMessage);
            ToastManager.show('Upload Failed', errorMessage, 'danger');
        })
        .finally(() => {
            this.state.uploadInProgress = false;
            // Reset the file input so the user can select the same file again
            this.state.fileInput.value = '';
        });
    }
};

const MentionManager = {
    state: {},

    initialize: function(editorState) {
        const popoverContainer = document.createElement('div');
        popoverContainer.id = 'mention-popover-container';
        popoverContainer.className = 'border rounded shadow-sm bg-body';
        popoverContainer.style.position = 'absolute';
        popoverContainer.style.bottom = '100%';
        popoverContainer.style.left = '40px';
        popoverContainer.style.zIndex = '1060';
        popoverContainer.style.display = 'none';
        popoverContainer.style.maxHeight = '250px';
        popoverContainer.style.width = '300px';
        popoverContainer.style.overflowY = 'auto';

        // Append the container to the form so it's positioned correctly
        editorState.messageForm.appendChild(popoverContainer);


        this.state = {
            editorState: editorState,
            popoverContainer: popoverContainer,
            active: false,
            triggerPosition: -1,
            query: '',
            isSelecting: false
        };
        this.bindEvents();
    },

    bindEvents: function() {
        const { editor, markdownView } = this.state.editorState;
        editor.addEventListener('input', this.handleInput.bind(this));
        markdownView.addEventListener('input', this.handleInput.bind(this));

        editor.addEventListener('keydown', this.handleKeyDown.bind(this));
        markdownView.addEventListener('keydown', this.handleKeyDown.bind(this));

        // Listen for clicks on suggestion items (delegated)
        this.state.popoverContainer.addEventListener('click', (e) => {
            const item = e.target.closest('.mention-suggestion-item');
            if (item) {
                this.selectItem(item);
            }
        });
    },

    handleInput: function() {
        // This flag prevents the input event we fire in selectItem from re-triggering a search.
        if (this.state.isSelecting) {
            this.state.isSelecting = false;
            return;
        }

        const { isMarkdownMode, markdownView, editor } = this.state.editorState;
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
            this.state.query = textUpToCursor.substring(triggerIndex + 1);
            this.state.triggerPosition = triggerIndex;

            // If the user types a space, the mention is complete, so hide the popover.
            if (this.state.query.includes(' ')) {
                this.hidePopover();
                return;
            }

            this.fetchUsers(this.state.query);
        } else {
            this.hidePopover();
        }
    },

    handleKeyDown: function(e) {
        if (!this.state.active) return;

        const items = this.state.popoverContainer.querySelectorAll('.mention-suggestion-item');
        if (items.length === 0) return;

        let activeItem = this.state.popoverContainer.querySelector('.mention-suggestion-item.active');

        // Add 'Escape' to the list of keys we want to control.
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
                this.selectItem(activeItem);
            } else if (e.key === 'Escape') {
                this.hidePopover();
            }
        }
    },

    fetchUsers: function(query) {
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        if (!activeConv) {
            this.hidePopover();
            return;
        }
        const conversationIdStr = activeConv.dataset.conversationId;
        const url = `/chat/conversation/${conversationIdStr}/mention_search?q=${encodeURIComponent(query)}`;

        htmx.ajax('GET', url, { target: this.state.popoverContainer, swap: 'innerHTML' });
        this.showPopover();
    },

    selectItem: function(item) {
        this.state.isSelecting = true; // Set the flag before changing the input
        const username = item.dataset.username;
        const { isMarkdownMode, markdownView, editor } = this.state.editorState;

        if (isMarkdownMode) {
            const text = markdownView.value;
            const pre = text.substring(0, this.state.triggerPosition);
            const post = text.substring(markdownView.selectionStart);
            markdownView.value = `${pre}@${username} ${post}`;
            const newCursorPos = (pre + `@${username} `).length;
            markdownView.focus();
            markdownView.setSelectionRange(newCursorPos, newCursorPos);
        } else { // ContentEditable
            editor.focus();
            const selection = window.getSelection();
            const range = selection.getRangeAt(0);

            // This logic is tricky. We need to find the correct text node.
            let textNode = range.startContainer;
            range.setStart(textNode, this.state.triggerPosition);
            range.setEnd(textNode, range.startOffset);
            range.deleteContents();
            document.execCommand('insertText', false, `@${username} `);
        }

        this.hidePopover();
        // Trigger input event so the editor resizes etc.
        (isMarkdownMode ? markdownView : editor).dispatchEvent(new Event('input', { bubbles: true }));
    },

    showPopover: function() {
        this.state.popoverContainer.style.display = 'block';
        this.state.active = true;
    },

    hidePopover: function() {
        this.state.popoverContainer.style.display = 'none';
        this.state.popoverContainer.innerHTML = '';
        this.state.active = false;
    }
};


// --- 1. THE CHAT INPUT EDITOR ---
const Editor = {
    // This state object holds all variables and element references for the editor instance.
    state: {},

    /**
     * Initializes the chat editor. Gathers elements and sets up all event listeners.
     */
    initialize: function() {
        const messageForm = document.getElementById('message-form');
        if (!messageForm) return;

        const elements = {
            messageForm: messageForm,
            editor: document.getElementById('wysiwyg-editor'),
            markdownView: document.getElementById('markdown-toggle-view'),
            hiddenInput: document.getElementById('chat-message-input'),
            topToolbar: document.querySelector('.wysiwyg-toolbar:not(.wysiwyg-toolbar-bottom)'),
            formatToggleButton: document.getElementById('format-toggle-btn'),
            sendButton: document.getElementById('send-button'),
            typingSender: document.getElementById('typing-sender'),
            blockquoteButton: document.querySelector('[data-command="formatBlock"][data-value="blockquote"]'),
            emojiButton: document.getElementById('emoji-btn'),
            emojiPicker: document.querySelector('emoji-picker')
        };

        if (Object.values(elements).some(el => !el)) {
            console.error("Chat Editor initialization failed: one or more required elements were not found.");
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
            replacement: content => `~~${content}~~`
        });

        this.state = {
            ...elements,
            turndownService,
            isMarkdownMode,
            typingTimer: null
        };

        this.setupEmojiPickerListeners();
        this.setupToolbarListener();
        this.setupInputListeners();
        this.setupKeydownListeners();
        this.setupFormListeners();
        this.setupToggleButtonListener();
        this.updateView();
        this.initializeMentions();
        AttachmentManager.initialize(this.state);
    },

    /**
     * Pre-processes raw markdown text to insert blank lines between blockquotes
     * and subsequent paragraphs, making it compliant with the strict Markdown spec.
     * @param {string} text The raw text from the markdown view.
     * @returns {string} The processed text with necessary blank lines.
     */
    preprocessMarkdown: function(text) {
        const lines = text.split('\n');
        const processedLines = [];
        for (let i = 0; i < lines.length; i++) {
            processedLines.push(lines[i]);
            // Check if the current line is a blockquote...
            const isQuote = lines[i].trim().startsWith('>');
            // ...and if the next line exists, is not empty, and is NOT a blockquote.
            if (isQuote && (i + 1 < lines.length) && lines[i + 1].trim() !== '' && !lines[i + 1].trim().startsWith('>')) {
                // If all conditions are met, insert a blank line.
                processedLines.push('');
            }
        }
        return processedLines.join('\n');
    },

    /* insert text into the chat window on clicks (mainly for emojis) */
    insertText: function(text) {
        const { editor, markdownView, isMarkdownMode } = this.state;
        if (isMarkdownMode) {
            // For the textarea, we can just insert at the cursor position
            const start = markdownView.selectionStart;
            const end = markdownView.selectionEnd;
            const currentText = markdownView.value;
            markdownView.value = currentText.substring(0, start) + text + currentText.substring(end);
            markdownView.focus();
            // Move cursor to after the inserted text
            markdownView.selectionStart = markdownView.selectionEnd = start + text.length;
        } else {
            // For the contenteditable div, the 'insertText' command is the standard way
            editor.focus();
            document.execCommand('insertText', false, text);
        }
        // Manually trigger the input event so other functions (like resizing) run
        const activeInput = isMarkdownMode ? markdownView : editor;
        activeInput.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
    },

    /**
     * Sets focus to the currently active input (either Markdown or WYSIWYG).
     */
    focusActiveInput: function() {
        const { editor, markdownView, isMarkdownMode } = this.state;
        const activeInput = isMarkdownMode ? markdownView : editor;
        // The timeout ensures the browser has finished rendering before we try to focus.
        if (activeInput && typeof activeInput.focus === 'function') {
            // The timeout ensures the browser has finished rendering before we try to focus.
            setTimeout(() => activeInput.focus(), 0);
        }
    },

    // --- UI Update Functions ---
    updateView: function() {
        const { editor, markdownView, topToolbar, isMarkdownMode } = this.state;
        if (isMarkdownMode) {
            editor.style.display = 'none';
            markdownView.style.display = 'block';
            topToolbar.classList.add('toolbar-hidden');
            markdownView.focus();
        } else {
            markdownView.style.display = 'none';
            editor.style.display = 'block';
            topToolbar.classList.remove('toolbar-hidden');
            editor.focus();
        }
        this.resizeActiveInput();
        this.updateSendButton();
    },
    updateSendButton: function() {
        const { sendButton, isMarkdownMode, messageForm } = this.state;
        const isReply = messageForm.querySelector('[name="parent_message_id"]');
        const sendText = isReply ? "Reply" : "Send";

        if (isMarkdownMode) {
            sendButton.innerHTML = `<span>${sendText}</span><span class="send-shortcut"><i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = `${sendText} (Enter)`;
        } else {
            sendButton.innerHTML = `<span>${sendText}</span><span class="send-shortcut"><kbd>Ctrl</kbd>+<i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = `${sendText} (Ctrl+Enter)`;
        }
    },
    resizeActiveInput: function() {
        const { editor, markdownView, isMarkdownMode } = this.state;
        const activeInput = isMarkdownMode ? markdownView : editor;
        // trying to do the height calculation after the htmx update
        setTimeout(() => {
            if (activeInput) {
                activeInput.style.height = 'auto';
                activeInput.style.height = `${activeInput.scrollHeight}px`;
            }
        }, 0);
    },
    updateStateAndButtons: function() {
        const { editor, hiddenInput, turndownService, blockquoteButton, topToolbar } = this.state;
        const htmlContent = editor.innerHTML;
        const markdownContent = turndownService.turndown(htmlContent).trim();
        hiddenInput.value = markdownContent;
        const commands = ['bold', 'italic', 'strikethrough', 'insertUnorderedList', 'insertOrderedList'];
        commands.forEach(cmd => {
            const btn = topToolbar.querySelector(`[data-command="${cmd}"]`);
            if (btn) btn.classList.toggle('active', document.queryCommandState(cmd));
        });
        if (blockquoteButton) {
            blockquoteButton.classList.toggle('active', this.isSelectionInBlockquote());
        }
    },
    isSelectionInBlockquote: function() {
        const selection = window.getSelection();
        if (!selection.rangeCount) return false;
        let node = selection.getRangeAt(0).startContainer;
        if (node.nodeType === 3) node = node.parentNode;
        while (node && node.id !== 'wysiwyg-editor') {
            if (node.nodeName === 'BLOCKQUOTE') return true;
            node = node.parentNode;
        }
        return false;
    },
    sendTypingStatus: function(isTyping) {
        const { typingSender } = this.state;
        const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        if (activeConv) {
            const payload = { type: isTyping ? 'typing_start' : 'typing_stop', conversation_id: activeConv.dataset.conversationId };
            typingSender.setAttribute('hx-vals', JSON.stringify(payload));
            htmx.trigger(typingSender, 'typing-event');
        }
    },

    // --- Event Listener Setup Functions ---
    setupEmojiPickerListeners: function() {
        const { emojiButton, emojiPicker } = this.state;
        const pickerContainer = document.getElementById('emoji-picker-container');

        // Toggle picker visibility when the button is clicked
        emojiButton.addEventListener('click', (e) => {
            e.stopPropagation(); // Prevent the document click listener from firing immediately
            const isHidden = pickerContainer.style.display === 'none';
            pickerContainer.style.display = isHidden ? 'block' : 'none';
        });

        // Insert the selected emoji's unicode character when an emoji is picked
        emojiPicker.addEventListener('emoji-click', event => {
            this.insertText(event.detail.unicode);
            // Hide picker after selection
            pickerContainer.style.display = 'none';
        });
    },
    setupToolbarListener: function() {
        this.state.topToolbar.addEventListener('mousedown', e => {
            e.preventDefault();
            const button = e.target.closest('button');
            if (!button) return;
            if (button.dataset.value === 'blockquote') {
                const format = this.isSelectionInBlockquote() ? 'div' : 'blockquote';
                document.execCommand('formatBlock', false, format);
            } else {
                const { command, value } = button.dataset;
                document.execCommand(command, false, value);
            }
            this.state.editor.focus();
            this.updateStateAndButtons();
        });
    },
    setupInputListeners: function() {
        this.state.editor.addEventListener('input', () => {
            this.updateStateAndButtons();
            this.resizeActiveInput();
            clearTimeout(this.state.typingTimer);
            this.sendTypingStatus(true);
            this.state.typingTimer = setTimeout(() => this.sendTypingStatus(false), 1500);
        });
        this.state.markdownView.addEventListener('input', () => {
            this.resizeActiveInput();
            clearTimeout(this.state.typingTimer);
            this.sendTypingStatus(true);
            this.state.typingTimer = setTimeout(() => this.sendTypingStatus(false), 1500);
        });
        document.addEventListener('selectionchange', () => {
            if (document.activeElement === this.state.editor) {
                this.updateStateAndButtons();
            }
        });
    },
    setupKeydownListeners: function() {
        const currentUserId = document.querySelector('main.main-content').dataset.currentUserId;
        const keydownHandler = (e) => {
            // If the mention popover is active, do nothing and let it handle the event.
            if (MentionManager.state.active) {
                return;
            }
            if (!this.state.isMarkdownMode && e.key === 'Enter' && !e.shiftKey) {
                const selection = window.getSelection();
                if (selection.rangeCount) {
                    const range = selection.getRangeAt(0);
                    const node = range.startContainer;
                    if (this.isSelectionInBlockquote() && node.textContent.trim() === '' && range.startOffset === node.textContent.length) {
                        e.preventDefault();
                        document.execCommand('formatBlock', false, 'div');
                        return;
                    }
                }
            }
            const currentContent = this.state.isMarkdownMode ? this.state.markdownView.value : this.state.editor.innerText;
            if (e.key === 'ArrowUp' && currentContent.trim() === '') {
                e.preventDefault();
                const lastUserMessage = document.querySelector(`.message-container[data-user-id="${currentUserId}"]:last-of-type`);
                if (lastUserMessage) {
                    const editButton = lastUserMessage.querySelector('.message-toolbar button[data-action="edit"]');
                    if (editButton) htmx.trigger(editButton, 'click');
                }
                return;
            }
            if (e.key === 'Enter') {
                if (this.state.isMarkdownMode && !e.shiftKey) {
                    e.preventDefault();
                    if (this.state.markdownView.value.trim() !== '') this.state.messageForm.requestSubmit();
                } else if (!this.state.isMarkdownMode && e.ctrlKey) {
                    e.preventDefault();
                    if (this.state.editor.innerText.trim() !== '') this.state.messageForm.requestSubmit();
                }
            }
        };
        this.state.editor.addEventListener('keydown', keydownHandler);
        this.state.markdownView.addEventListener('keydown', keydownHandler);
    },
    setupFormListeners: function() {
        this.state.messageForm.addEventListener('submit', (e) => {
            if (this.state.isMarkdownMode) {
                const rawMarkdown = this.state.markdownView.value;
                this.state.hiddenInput.value = this.preprocessMarkdown(rawMarkdown);
            } else {
                this.updateStateAndButtons();
            }

            // A message is valid if it has text OR an attachment
            const hasText = this.state.hiddenInput.value.trim() !== '';
            const hasAttachment = document.getElementById('attachment-file-id').value !== '';

            if (!hasText && !hasAttachment) {
                e.preventDefault();
                return;
            }

            clearTimeout(this.state.typingTimer);
            this.sendTypingStatus(false);
        });

        this.state.messageForm.addEventListener('htmx:wsAfterSend', () => {
            const isReply = this.state.messageForm.querySelector('[name="parent_message_id"]');

            if (isReply) {
                htmx.ajax('GET', '/chat/input/default', {
                    target: '#chat-input-container',
                    swap: 'outerHTML'
                });
            } else {
                const { editor, markdownView, hiddenInput } = this.state;
                editor.innerHTML = '';
                markdownView.value = '';
                hiddenInput.value = '';
                // reset the upload for the next one
                document.getElementById('attachment-file-id').value = '';
                this.state.messageForm.setAttribute('ws-send', '');
                this.resizeActiveInput();
                this.focusActiveInput();
            }
        });
    },
    setupToggleButtonListener: function() {
        this.state.formatToggleButton.addEventListener('click', () => {
            const { editor, markdownView, isMarkdownMode, turndownService } = this.state;

            // Content preservation logic
            if (isMarkdownMode) {
                // Switching from Markdown to WYSIWYG
                const markdownContent = markdownView.value;
                if (markdownContent.trim() !== '') {
                    // Use our new backend endpoint to convert MD to HTML
                    htmx.ajax('POST', '/chat/utility/markdown-to-html', {
                        values: { text: markdownContent },
                        // The target is the editor div, but we only swap its content
                        target: editor,
                        swap: 'innerHTML'
                    }).then(() => {
                        this.updateStateAndButtons();
                    });
                } else {
                    editor.innerHTML = '';
                }
            } else {
                // Switching from WYSIWYG to Markdown
                const htmlContent = editor.innerHTML;
                if (editor.innerText.trim() !== '') {
                    markdownView.value = turndownService.turndown(htmlContent);
                } else {
                    markdownView.value = '';
                }
            }

            this.state.isMarkdownMode = !this.state.isMarkdownMode;
            this.updateView();

            const wysiwygIsEnabled = !this.state.isMarkdownMode;
            htmx.ajax('PUT', '/chat/user/preference/wysiwyg', {
                values: { 'wysiwyg_enabled': wysiwygIsEnabled },
                swap: 'none'
            });
        });
    },

    /* Mentions popover */
    initializeMentions: function() {
        const mentionButton = document.getElementById('mention-btn');
        if (!mentionButton) return;

        // Pass the main editor's state to the MentionManager
        MentionManager.initialize(this.state);

        mentionButton.addEventListener('click', () => {
            this.insertText('@');
            // Manually trigger the input event to open the popover
            const activeInput = this.state.isMarkdownMode ? this.state.markdownView : this.state.editor;
            activeInput.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
        });
    }
};

// --- 2. GENERAL PAGE-LEVEL LOGIC ---
document.addEventListener('DOMContentLoaded', () => {
    NotificationManager.initialize();

    const ToastManager = {
        toastEl: document.getElementById('app-toast'),
        headerEl: document.getElementById('toast-header'),
        titleEl: document.getElementById('toast-title'),
        bodyEl: document.getElementById('toast-body-content'),
        bootstrapToast: null,
        initialize: function() { if (this.toastEl) this.bootstrapToast = new bootstrap.Toast(this.toastEl, { delay: 5000 }); },
        show: function(title, message, level = 'danger', autohide = true) {
            if (!this.bootstrapToast || !this.titleEl || !this.bodyEl) return;
            this.titleEl.textContent = title;
            this.bodyEl.textContent = message;
            this.headerEl.className = 'toast-header';
            this.headerEl.classList.add(`bg-${level}`, 'text-white');
            this.bootstrapToast = new bootstrap.Toast(this.toastEl, { autohide: autohide, delay: 5000 });
            this.bootstrapToast.show();
        },
        hide: function() { if (this.bootstrapToast) { this.bootstrapToast.hide(); } }
    };
    ToastManager.initialize();

    // --- [CONSOLIDATED] Global Search and Interaction Logic ---
    const searchInput = document.getElementById('global-search-input');
    const searchOverlay = document.getElementById('search-results-overlay');
    const channelSidebar = document.querySelector('.channel-sidebar');

    const hideSearch = () => {
        if (!searchOverlay || !searchInput) return;
        searchOverlay.style.display = 'none';
        searchInput.value = '';
        htmx.trigger(searchInput, 'htmx:abort');
        //searchOverlay.innerHTML = '';
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

    // Intercept reply clicks to preserve draft content.
    document.body.addEventListener('htmx:configRequest', function(evt) {
        const trigger = evt.detail.elt;
        const targetId = evt.detail.target.id;

        // Check if a reply button was clicked and it's targeting the main input
        if (targetId === 'chat-input-container' && trigger.dataset.action === 'reply') {
            let draftContent = '';
            if (Editor && Editor.state) {
                if (Editor.state.isMarkdownMode) {
                    draftContent = Editor.state.markdownView.value;
                } else {
                    // Update state to get the latest markdown from the WYSIWYG editor
                    Editor.updateStateAndButtons();
                    draftContent = Editor.state.hiddenInput.value;
                }
            }

            if (draftContent.trim() !== '') {
                // Add the draft content as a query parameter to the GET request
                evt.detail.parameters['draft'] = draftContent;
            }
        }
    });

    document.body.addEventListener('htmx:responseError', function(evt) {
        if (evt.detail.target.tagName === 'MAIN' && evt.detail.target.hasAttribute('ws-connect')) return;
        if (evt.detail.xhr.responseText) {
            ToastManager.show('An Error Occurred', evt.detail.xhr.responseText, 'danger');
        }
    });

    document.body.addEventListener('htmx:oobErrorNoTarget', function(evt) {
        const targetId = evt.detail.content.id || 'unknown';
        if (targetId.startsWith('status-dot-')) {
            console.log(`Ignoring harmless presence update for target: #${targetId}`);
            return;
        }
        const errorMessage = `A UI update failed because the target '#${targetId}' could not be found.`;
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
            const subscribeMsg = { type: "subscribe", conversation_id: activeConv.dataset.conversationId };
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
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage) {
            lastMessage.scrollIntoView({ behavior: "smooth", block: "end" });
        }
    };

    const scrollToBottomForce = () => {
        if (messagesContainer) {
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }
    };

    /** Helper Functions for UI initialization **/
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
            const popover = new bootstrap.Popover(popoverTriggerEl, {
                html: true,
                sanitize: false,
                content: `<emoji-picker class="light"></emoji-picker>`,
                placement: 'left',
                customClass: 'emoji-popover'
            });
            popoverTriggerEl.addEventListener('shown.bs.popover', () => {
                const picker = document.querySelector(`.popover[role="tooltip"] emoji-picker`);
                if (picker) {
                    const pickerListener = event => {
                        htmx.ajax('POST', `/chat/message/${messageId}/react`, { values: { emoji: event.detail.unicode }, swap: 'none' });
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
            // Get the message ID from the element's id attribute (e.g., "message-123")
            const messageId = parseInt(mentionEl.id.split('-')[1]);
            if (messageId > latestMentionId) {
                latestMentionId = messageId;
            }

            // After a delay, remove the highlight class to trigger the CSS fade-out.
            setTimeout(() => {
                mentionEl.classList.remove('mentioned-message');
            }, 3000); // 3-second delay
        });

        // If we found new mentions, send an update to the server so they don't
        // get highlighted again on the next page load.
        if (latestMentionId > 0 && conversationId) {
            const updateUrl = `/chat/conversation/${conversationId}/seen_mentions`;
            // We use a small, temporary HTMX element to send the request declaratively.
            const triggerEl = document.createElement('div');
            triggerEl.setAttribute('hx-post', updateUrl);
            triggerEl.setAttribute('hx-vals', `{"last_message_id": ${latestMentionId}}`);
            triggerEl.setAttribute('hx-trigger', 'load');
            document.body.appendChild(triggerEl);
            htmx.process(triggerEl);
            // Clean up the temporary element after it fires.
            setTimeout(() => document.body.removeChild(triggerEl), 100);
        }
    };

    /** Main Event Listeners **/
    document.body.addEventListener('focus-chat-input', () => { if (Editor && typeof Editor.focusActiveInput === 'function') Editor.focusActiveInput(); });
    document.body.addEventListener('chatInputLoaded', () => Editor.initialize());
    document.addEventListener('click', (e) => {
        const pickerContainer = document.getElementById('emoji-picker-container');
        if (pickerContainer && pickerContainer.style.display === 'block') {
            if (!pickerContainer.contains(e.target) && !e.target.closest('#emoji-btn')) {
                pickerContainer.style.display = 'none';
            }
        }
    });

    document.body.addEventListener('htmx:beforeSwap', (evt) => {
        // We are listening for the moment right before HTMX swaps in the content
        // for a new channel or DM.
        //
        // By clearing the typing indicator here, we prevent a stale "user is typing"
        // message from the *previous* conversation from getting stuck on the screen
        // after the user has switched to a new one.
        if (evt.detail.target.id === 'chat-messages-container' && evt.detail.requestConfig.verb === 'get') {
            const typingIndicator = document.getElementById('typing-indicator');
            if (typingIndicator) {
                typingIndicator.innerHTML = '';
            }
        }
    });

    let userWasNearBottom = false;
    document.body.addEventListener('htmx:wsBeforeMessage', function() {
        userWasNearBottom = isUserNearBottom();
    });

// This is the complete, consolidated listener (around line 812)
    document.body.addEventListener('htmx:afterSwap', (event) => {
        const target = event.detail.target;

        // --- Logic for Search ---
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
            // Early return after handling search
            return;
        }

        // --- Logic for all other swaps (chat, modals, etc.) ---
        processCodeBlocks(target);
        initializeReactionPopovers(target);
        updateReactionHighlights(target);
        initializeTooltips(target);

        // This block handles initial channel/DM loads
        if (target.id === 'chat-messages-container' && event.detail.requestConfig.verb === 'get') {
            setTimeout(scrollToBottomForce, 50);
            if(jumpToBottomBtn) jumpToBottomBtn.style.display = 'none';
            Editor.focusActiveInput();

            // Check for and handle any new mention highlights
            const conversationDiv = target.querySelector('[data-conversation-db-id]');
            if (conversationDiv) {
                const conversationId = conversationDiv.dataset.conversationDbId;
                handleMentionHighlights(target, conversationId);
            }
        }
    });

    document.body.addEventListener('htmx:wsAfterMessage', (event) => {
        try {
            const data = JSON.parse(event.detail.message);
            if (typeof data === 'object' && data.type) {
                if (data.type === 'avatar_update') {
                    const { user_id, avatar_url } = data;
                    const avatarImages = document.querySelectorAll(`.avatar-image[data-user-id="${user_id}"]`);
                    avatarImages.forEach(img => {
                        img.src = avatar_url;
                    });
                    return;
                }
                if (data.type === 'notification') NotificationManager.showNotification(data);
                else if (data.type === 'sound') NotificationManager.playSound();
                return;
            }
        } catch (e) { /* Fall through to process as HTML */ }

        // --- [THE FIX] Start of new logic ---

        const mainContent = document.querySelector('main.main-content');
        const currentUserId = mainContent.dataset.currentUserId;
        const currentUsername = mainContent.dataset.currentUsername;
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');

        if (lastMessage) {
            const messageAuthorId = lastMessage.dataset.userId;
            const messageContentEl = lastMessage.querySelector('.message-content');
            const messageContent = messageContentEl ? messageContentEl.innerText : '';

            // 1. Handle mention highlighting
            // Check if the message is NOT from the current user and contains a mention of them.
            // The \b ensures we match whole words only (e.g., @kenny not @kenny_extra).
            const mentionRegex = new RegExp(`@(${currentUsername}|here|channel)\\b`, 'i');
            if (messageAuthorId !== currentUserId && mentionRegex.test(messageContent)) {
                lastMessage.classList.add('mentioned-message');
            }

            // 2. Handle scrolling
            if (messageAuthorId === currentUserId) {
                setTimeout(scrollLastMessageIntoView, 0);
            } else {
                if (userWasNearBottom) {
                    setTimeout(scrollLastMessageIntoView, 0);
                } else {
                    if (jumpToBottomBtn) jumpToBottomBtn.style.display = 'block';
                }
            }

            // 3. Initialize dynamic components on the new message
            initializeReactionPopovers(lastMessage);
            processCodeBlocks(lastMessage);

            // 4. This will handle the fade-out for any message that has the highlight class.
            handleMentionHighlights(lastMessage, null);
        }
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
        htmxModalEl.addEventListener('hidden.bs.modal', () => {
            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) modalContent.innerHTML = `<div class="modal-body text-center"><div class="spinner-border" role="status"></div></div>`;
        });
    }

    const rightPanelOffcanvasEl = document.getElementById('right-panel-offcanvas');
    if (rightPanelOffcanvasEl) {
        const rightPanelOffcanvas = new bootstrap.Offcanvas(rightPanelOffcanvasEl);
        document.body.addEventListener('close-offcanvas', () => rightPanelOffcanvas.hide());
    }

    // This listener handles closing the top-most active UI element when the Escape key is pressed.
    document.body.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            // 1. Check for search overlay
            if (searchOverlay && searchOverlay.style.display !== 'none') {
                e.preventDefault();
                const closeBtn = document.getElementById('close-search-btn');
                if (closeBtn) closeBtn.click();
                return;
            }

            // 2. [THE FIX] Check for an open emoji reaction popover
            const visiblePopover = document.querySelector('.popover.show');
            if (visiblePopover) {
                e.preventDefault();
                // Find the button that triggered this popover
                const triggerEl = document.querySelector(`[aria-describedby="${visiblePopover.id}"]`);
                if (triggerEl) {
                    const popoverInstance = bootstrap.Popover.getInstance(triggerEl);
                    if (popoverInstance) {
                        popoverInstance.hide();
                    }
                }
                return;
            }

            // 3. Check for the slide-out right panel
            if (rightPanelOffcanvasEl && rightPanelOffcanvasEl.classList.contains('show')) {
                 e.preventDefault();
                 const offcanvasInstance = bootstrap.Offcanvas.getInstance(rightPanelOffcanvasEl);
                 if (offcanvasInstance) offcanvasInstance.hide();
                 return;
            }

            // 4. Check for the main HTMX modal
            if (htmxModalEl && htmxModalEl.classList.contains('show')) {
                e.preventDefault();
                const modalInstance = bootstrap.Modal.getInstance(htmxModalEl);
                if (modalInstance) modalInstance.hide();
                return;
            }

            // 5. Check for the main chat input's emoji picker
            const emojiPickerContainer = document.getElementById('emoji-picker-container');
             if (emojiPickerContainer && emojiPickerContainer.style.display !== 'none') {
                e.preventDefault();
                emojiPickerContainer.style.display = 'none';
                return;
            }
        }
    });

    // Initializations on page load
    processCodeBlocks(document.body);
    initializeReactionPopovers(document.body);
    updateReactionHighlights(document.body);
    initializeTooltips(document.body);
});
