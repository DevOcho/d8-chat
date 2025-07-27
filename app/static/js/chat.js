/* FINAL REFACTORED CODE: app/static/js/chat.js */

// We wrap everything in a DOMContentLoaded listener to ensure the HTML is ready.
document.addEventListener('DOMContentLoaded', () => {
    // --- Element References ---
    const messagesContainer = document.getElementById('chat-messages-container');
    const jumpToBottomBtn = document.getElementById('jump-to-bottom-btn');
    const mainContent = document.querySelector('main.main-content');
    const currentUserId = mainContent.dataset.currentUserId;

    /**
     * This function finds the chat input form and attaches all necessary
     * event listeners for resizing, typing indicators, and shortcuts.
     * It is designed to be called every time a new input form is loaded.
     */
    const initializeChatInput = () => {
        const messageForm = document.getElementById('message-form');
        const messageInput = document.getElementById('chat-message-input');
        const typingSender = document.getElementById('typing-sender');

        if (!messageForm || !messageInput || !typingSender) return;

        // --- Focus the input ---
        messageInput.focus();

        const doneTypingInterval = 1500;
        let typingTimer;
        const initialTextareaHeight = messageInput.scrollHeight;

        const resizeTextarea = () => {
            messageInput.style.height = 'auto';
            const newHeight = Math.max(initialTextareaHeight, messageInput.scrollHeight);
            messageInput.style.height = `${newHeight}px`;
        };

        messageInput.addEventListener('input', resizeTextarea);
        resizeTextarea();

        messageInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (messageInput.value.trim() !== '') htmx.trigger(messageForm, 'submit');
            }
            if (e.key === 'ArrowUp' && messageInput.value.trim() === '') {
                e.preventDefault();
                const lastUserMessage = document.querySelector(`.message-container[data-user-id="${currentUserId}"]:last-of-type`);
                if (lastUserMessage) {
                    const editButton = lastUserMessage.querySelector('.message-toolbar button[data-action="edit"]');
                    if (editButton) htmx.trigger(editButton, 'click');
                }
            }
        });

        const sendTypingStatus = (isTyping) => {
            const activeConv = document.querySelector('#chat-messages-container > div[data-conversation-id]');
            if (activeConv) {
                const payload = { type: isTyping ? 'typing_start' : 'typing_stop', conversation_id: activeConv.dataset.conversationId };
                typingSender.setAttribute('hx-vals', JSON.stringify(payload));
                htmx.trigger(typingSender, 'typing-event');
            }
        };

        messageInput.addEventListener('input', () => {
            clearTimeout(typingTimer);
            sendTypingStatus(true);
            typingTimer = setTimeout(() => sendTypingStatus(false), doneTypingInterval);
        });

        messageForm.addEventListener('submit', () => {
            clearTimeout(typingTimer);
            sendTypingStatus(false);
        });

        messageForm.addEventListener('htmx:wsAfterSend', () => {
            if (!document.querySelector('#chat-input-container .quoted-reply')) {
                messageForm.reset();
                resizeTextarea();
                messageInput.focus();
            }
        });
    };

    // --- MAIN PAGE LOGIC ---
    // Listen for our custom event to initialize the chat input scripts.
    document.body.addEventListener('chatInputLoaded', initializeChatInput);

    const isUserNearBottom = () => {
        return messagesContainer.scrollHeight - messagesContainer.clientHeight - messagesContainer.scrollTop < 150;
    };

    const scrollLastMessageIntoView = () => {
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage) lastMessage.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
            jumpToBottomBtn.style.display = 'none';
        }

       // After the new chat is loaded, find the input and focus it.
       const messageInput = document.getElementById('chat-message-input');
       if (messageInput) {
           messageInput.focus();
       }
    });

    document.body.addEventListener('htmx:wsAfterMessage', (event) => {
        const lastMessage = document.querySelector('#message-list > .message-container:last-child');
        if (lastMessage) {
            setTimeout(() => processCodeBlocks(lastMessage), 50);
        }
        if (isUserNearBottom()) {
            scrollLastMessageIntoView();
        } else {
            jumpToBottomBtn.style.display = 'block';
        }
    });

    messagesContainer.addEventListener('scroll', () => {
        if (isUserNearBottom()) jumpToBottomBtn.style.display = 'none';
    });

    jumpToBottomBtn.addEventListener('click', () => {
        setTimeout(() => messagesContainer.scrollTop = messagesContainer.scrollHeight, 50);
        jumpToBottomBtn.style.display = 'none';
    });

    const createChannelModalEl = document.getElementById('createChannelModal');
    if (createChannelModalEl) {
        const createChannelModal = new bootstrap.Modal(createChannelModalEl);
        document.body.addEventListener('close-create-channel-modal', () => createChannelModal.hide());
        createChannelModalEl.addEventListener('hidden.bs.modal', () => {
            document.getElementById('createChannelModalContent').innerHTML = `<div class="modal-body text-center"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div>`;
        });
    }

    processCodeBlocks(document.body);
});
