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

        const turndownService = new TurndownService({
            headingStyle: 'atx',
            codeBlockStyle: 'fenced',
            br: '\n'
        });
        turndownService.addRule('strikethrough', {
            filter: ['del', 's', 'strike'],
            replacement: content => `~${content}~`
        });

        this.state = {
            ...elements,
            turndownService,
            isMarkdownMode: true,
            typingTimer: null
        };

        this.setupEmojiPickerListeners();
        this.setupToolbarListener();
        this.setupInputListeners();
        this.setupKeydownListeners();
        this.setupFormListeners();
        this.setupToggleButtonListener();
        this.updateView();
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
        setTimeout(() => activeInput.focus(), 0);
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
        const { sendButton, isMarkdownMode } = this.state;
        if (isMarkdownMode) {
            sendButton.innerHTML = `<span>Send</span><span class="send-shortcut"><i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = "Send (Enter)";
        } else {
            sendButton.innerHTML = `<span>Send</span><span class="send-shortcut"><kbd>Ctrl</kbd>+<i class="bi bi-arrow-return-left"></i></span>`;
            sendButton.title = "Send (Ctrl+Enter)";
        }
    },
    resizeActiveInput: function() {
        // ... (this function is unchanged)
        const { editor, markdownView, isMarkdownMode } = this.state;
        const activeInput = isMarkdownMode ? markdownView : editor;
        activeInput.style.height = 'auto';
        activeInput.style.height = `${activeInput.scrollHeight}px`;
    },
    updateStateAndButtons: function() {
        // ... (this function is unchanged)
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
                // Pre-process the markdown before assigning it to the hidden input
                const rawMarkdown = this.state.markdownView.value;
                this.state.hiddenInput.value = this.preprocessMarkdown(rawMarkdown);
            } else {
                this.updateStateAndButtons();
            }
            if (this.state.hiddenInput.value.trim() === '') {
                e.preventDefault();
                return;
            }
            clearTimeout(this.state.typingTimer);
            this.sendTypingStatus(false);
        });

        this.state.messageForm.addEventListener('htmx:wsAfterSend', () => {
            if (!document.querySelector('#chat-input-container .quoted-reply')) {
                const { editor, markdownView, hiddenInput } = this.state;
                editor.innerHTML = '';
                markdownView.value = '';
                hiddenInput.value = '';
                editor.style.height = 'auto';
                markdownView.style.height = 'auto';
                this.state.isMarkdownMode ? markdownView.focus() : editor.focus();
            }
        });
    },
    setupToggleButtonListener: function() {
        this.state.formatToggleButton.addEventListener('click', () => {
            this.state.isMarkdownMode = !this.state.isMarkdownMode;
            this.updateView();
        });
    }
};


// --- 2. GENERAL PAGE-LEVEL LOGIC ---

document.addEventListener('DOMContentLoaded', () => {
    NotificationManager.initialize();

    // --- Element References ---
    const messagesContainer = document.getElementById('chat-messages-container');
    const jumpToBottomBtn = document.getElementById('jump-to-bottom-btn');

    // Listen for our custom event to initialize the chat editor scripts.
    document.body.addEventListener('chatInputLoaded', () => Editor.initialize());
    // Global click listener to hide the emoji picker if clicked outside
    document.addEventListener('click', (e) => {
        const pickerContainer = document.getElementById('emoji-picker-container');
        if (pickerContainer && pickerContainer.style.display === 'block') {
            // Hide if the click is outside the picker AND not on the toggle button
            if (!pickerContainer.contains(e.target) && !e.target.closest('#emoji-btn')) {
                pickerContainer.style.display = 'none';
            }
        }
    });

    const isUserNearBottom = () => {
        if (!messagesContainer) return false;
        return messagesContainer.scrollHeight - messagesContainer.clientHeight - messagesContainer.scrollTop < 150;
    };

    const scrollLastMessageIntoView = () => {
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage) lastMessage.scrollIntoView({ behavior: "smooth", block: "end" });
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

    document.body.addEventListener('htmx:afterSwap', (event) => {
        const target = event.detail.target;
        processCodeBlocks(target);
        if (target.id === 'chat-messages-container' && event.detail.requestConfig.verb === 'get') {
            scrollLastMessageIntoView();
            if(jumpToBottomBtn) jumpToBottomBtn.style.display = 'none';

            // [THE FIX]
            // After a new channel/DM is loaded into the main view,
            // we programmatically refocus the chat input for the user.
            // This restores the expected behavior of being able to type immediately.
            Editor.focusActiveInput();
        }
    });

    document.body.addEventListener('htmx:wsAfterMessage', (event) => {
        try {
            const data = JSON.parse(event.detail.message);
            if (typeof data === 'object' && data.type) {
                if (data.type === 'notification') {
                    NotificationManager.showNotification(data);
                } else if (data.type === 'sound') {
                    NotificationManager.playSound();
                }
                return; // Message was a JSON payload, stop processing.
            }
        } catch (e) {
            // Not JSON, fall through to process as HTML.
        }

        // Process as an HTML swap for the message list
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage) {
            setTimeout(() => processCodeBlocks(lastMessage), 50);
        }

        if (isUserNearBottom()) {
            scrollLastMessageIntoView();
        } else {
            if (jumpToBottomBtn) jumpToBottomBtn.style.display = 'block';
        }
    });

    if (messagesContainer) {
        messagesContainer.addEventListener('scroll', () => {
            if (isUserNearBottom() && jumpToBottomBtn) jumpToBottomBtn.style.display = 'none';
        });
    }

    if (jumpToBottomBtn) {
        jumpToBottomBtn.addEventListener('click', () => {
            setTimeout(() => messagesContainer.scrollTop = messagesContainer.scrollHeight, 50);
            jumpToBottomBtn.style.display = 'none';
        });
    }

    const htmxModalEl = document.getElementById('htmx-modal');
    if (htmxModalEl) {
        const htmxModal = new bootstrap.Modal(htmxModalEl);
        document.body.addEventListener('close-modal', () => htmxModal.hide());
        htmxModalEl.addEventListener('hidden.bs.modal', () => {
            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) modalContent.innerHTML = `<div class="modal-body text-center"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div>`;
        });
    }

    processCodeBlocks(document.body);
});
