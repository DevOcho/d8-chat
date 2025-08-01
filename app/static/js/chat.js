/* app/static/js/chat.js */

document.addEventListener('DOMContentLoaded', () => {
    // --- Element References ---
    const messagesContainer = document.getElementById('chat-messages-container');
    const jumpToBottomBtn = document.getElementById('jump-to-bottom-btn');
    const mainContent = document.querySelector('main.main-content');
    const currentUserId = mainContent ? mainContent.dataset.currentUserId : null;

    // Global variable to hold the editor instance
    let editor;

    const initializeChatInput = () => {
        // If an old editor instance exists, destroy it to prevent memory leaks
        if (editor) {
            editor.destroy();
        }

        const editorElement = document.getElementById('editor-content');
        const messageForm = document.getElementById('message-form');
        const messageInput = document.getElementById('chat-message-input'); // The hidden textarea

        if (!messageForm || !editorElement || !messageInput) {
            return; // Exit if the necessary elements aren't on the page
        }

        // --- Access libraries from the global window object ---
        const { Editor, StarterKit, CodeBlockLowlight, lowlight } = window.TipTap;

        // --- Create the Editor Instance ---
        editor = new Editor({
            element: editorElement,
            extensions: [
                StarterKit.configure({
                    heading: false,
                    //blockquote: false,
                    //horizontalRule: false,
                    codeBlock: false,
                }),
                CodeBlockLowlight.configure({
                    lowlight,
                }),
            ],
            editorProps: {
                attributes: {
                    class: 'ProseMirror form-control', // Use Bootstrap's styling
                },
            },
            // Use editorProps to add raw ProseMirror plugins and event handlers directly
            editorProps: {
                attributes: {
                    class: 'ProseMirror form-control',
                },
                // This is the most direct way to handle keyboard shortcuts
                handleKeyDown: (view, event) => {
                    // --- ENTER TO SUBMIT (if shift is not pressed) ---
                    if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        const messageForm = document.getElementById('message-form');
                        // Use the global 'editor' instance
                        if (messageForm && editor.getText().trim() !== '') {
                            htmx.trigger(messageForm, 'submit');
                        }
                        return true; // We've handled this event
                    }

                    // --- ARROW UP TO EDIT LAST MESSAGE (if editor is empty) ---
                    if (event.key === 'ArrowUp' && editor.isEmpty) {
                        event.preventDefault();
                        if (!currentUserId) return false;

                        const lastUserMessage = document.querySelector(`.message-container[data-user-id="${currentUserId}"]:last-of-type`);
                        if (lastUserMessage) {
                            const editButton = lastUserMessage.querySelector('.message-toolbar button[data-action="edit"]');
                            if (editButton) {
                                htmx.trigger(editButton, 'click');
                            }
                        }
                        return true; // We've handled this event
                    }

                    return false; // Let other handlers (like TipTap's own) run
                },
            },
            // On every editor update, sync its HTML content to our hidden textarea
            onUpdate: ({ editor }) => {
                messageInput.value = editor.getHTML();
            },
        });

        // --- Toolbar & Form Logic ---
        const toolbar = document.getElementById('tiptap-toolbar');
        toolbar.querySelector('#bold-btn').addEventListener('click', () => editor.chain().focus().toggleBold().run());
        toolbar.querySelector('#italic-btn').addEventListener('click', () => editor.chain().focus().toggleItalic().run());
        toolbar.querySelector('#bullet-list-btn').addEventListener('click', () => editor.chain().focus().toggleBulletList().run());
        toolbar.querySelector('#ordered-list-btn').addEventListener('click', () => editor.chain().focus().toggleOrderedList().run());
        toolbar.querySelector('#code-block-btn').addEventListener('click', () => editor.chain().focus().toggleCodeBlock().run());

        // Update button active states based on cursor position
        editor.on('transaction', () => {
            toolbar.querySelector('#bold-btn').classList.toggle('active', editor.isActive('bold'));
            toolbar.querySelector('#italic-btn').classList.toggle('active', editor.isActive('italic'));
            toolbar.querySelector('#bullet-list-btn').classList.toggle('active', editor.isActive('bulletList'));
            toolbar.querySelector('#ordered-list-btn').classList.toggle('active', editor.isActive('orderedList'));
            toolbar.querySelector('#code-block-btn').classList.toggle('active', editor.isActive('codeBlock'));
        });

        // Sync content one last time before submitting the form via HTMX/WebSocket
        messageForm.addEventListener('submit', () => {
            messageInput.value = editor.getHTML();
        });

        // After the WebSocket sends the message, clear the editor
        messageForm.addEventListener('htmx:wsAfterSend', () => {
            if (editor) {
                editor.commands.clearContent(true); // 'true' emits an update
                editor.commands.focus();
            }
        });

        // Focus the editor as soon as it's ready
        editor.commands.focus();
    };

    // --- MAIN PAGE LOGIC ---
    // Listen for our custom event to initialize/re-initialize the chat input scripts.
    document.body.addEventListener('chatInputLoaded', initializeChatInput);

    // ... scroll/modal logic ...
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
            if (jumpToBottomBtn) jumpToBottomBtn.style.display = 'none';
        }
        if (editor) {
            editor.commands.focus();
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
            if (messagesContainer) {
                 messagesContainer.scrollTo({ top: messagesContainer.scrollHeight, behavior: 'smooth' });
            }
            jumpToBottomBtn.style.display = 'none';
        });
    }
    
    const htmxModalEl = document.getElementById('htmx-modal');
    if (htmxModalEl) {
        const htmxModal = new bootstrap.Modal(htmxModalEl);
        document.body.addEventListener('close-modal', () => {
            htmxModal.hide();
        });
        htmxModalEl.addEventListener('hidden.bs.modal', () => {
            const modalContent = document.getElementById('htmx-modal-content');
            if (modalContent) {
                modalContent.innerHTML = `<div class="modal-body text-center"><div class="spinner-border" role="status"><span class="visually-hidden">Loading...</span></div></div>`;
            }
        });
    }

    processCodeBlocks(document.body);
});
