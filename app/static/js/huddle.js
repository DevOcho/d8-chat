/**
 * 1:1 WebRTC huddle (voice/video call) for DMs.
 *
 * Signaling rides the existing chat WebSocket (window.d8Socket, set by
 * chat.js on htmx:wsOpen) as typed JSON events — see the huddle_* branch in
 * app/routes.py::_process_ws_event, which relays these point-to-point to the
 * other DM participant via chat_manager.send_to_user. This file only owns
 * the RTCPeerConnection lifecycle and the call modal's DOM.
 */
const Huddle = (() => {
    const ICE_SERVERS = [{ urls: 'stun:stun.l.google.com:19302' }];
    // ponytail: public STUN only, no TURN server. Calls between peers behind
    // symmetric/restrictive NATs may fail to connect. Add TURN if that shows
    // up in practice.

    let modal = null;
    let modalEl, statusEl, remoteVideo, localVideo, incomingActions, activeActions, micBtn, camBtn;

    let state = 'idle'; // idle | ringing_out | ringing_in | connecting | connected
    let pc = null;
    let localStream = null;
    let callId = null;
    let peerName = null;
    let conversationId = null;
    let pendingOffer = null;
    let pendingCandidates = [];

    function send(type, extra) {
        if (!window.d8Socket) return;
        window.d8Socket.send(JSON.stringify({
            type, call_id: callId, conversation_id: conversationId, ...extra,
        }));
    }

    function ensureDom() {
        if (modal) return;
        modalEl = document.getElementById('huddle-modal');
        if (!modalEl) return;
        modal = new bootstrap.Modal(modalEl);
        statusEl = document.getElementById('huddle-status');
        remoteVideo = document.getElementById('huddle-remote-video');
        localVideo = document.getElementById('huddle-local-video');
        incomingActions = document.getElementById('huddle-incoming-actions');
        activeActions = document.getElementById('huddle-active-actions');
        micBtn = document.getElementById('huddle-toggle-mic-btn');
        camBtn = document.getElementById('huddle-toggle-camera-btn');
        // Covers ESC / backdrop dismissal, not just the Hang Up button.
        modalEl.addEventListener('hidden.bs.modal', () => {
            if (state !== 'idle') hangup();
        });
    }

    function reset() {
        if (pc) { pc.close(); pc = null; }
        if (localStream) { localStream.getTracks().forEach((t) => t.stop()); localStream = null; }
        state = 'idle';
        callId = null; peerName = null; conversationId = null;
        pendingOffer = null; pendingCandidates = [];
        if (remoteVideo) { remoteVideo.srcObject = null; remoteVideo.style.display = 'none'; }
        if (localVideo) { localVideo.srcObject = null; localVideo.style.display = 'none'; }
        if (incomingActions) incomingActions.style.display = 'none';
        if (activeActions) activeActions.style.display = 'none';
        if (micBtn) micBtn.className = 'btn btn-secondary me-2';
        if (camBtn) camBtn.className = 'btn btn-secondary me-2';
        if (modal) modal.hide();
    }

    function createPeerConnection() {
        const conn = new RTCPeerConnection({ iceServers: ICE_SERVERS });
        conn.onicecandidate = (e) => {
            if (e.candidate) send('huddle_ice', { candidate: e.candidate });
        };
        conn.ontrack = (e) => {
            remoteVideo.srcObject = e.streams[0];
            remoteVideo.style.display = 'block';
        };
        conn.onconnectionstatechange = () => {
            if (conn.connectionState === 'connected') {
                state = 'connected';
                statusEl.textContent = peerName ? `On a huddle with ${peerName}` : 'Connected';
            } else if (['failed', 'closed', 'disconnected'].includes(conn.connectionState) && state !== 'idle') {
                reset();
            }
        };
        return conn;
    }

    async function getMedia() {
        try {
            return await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
        } catch (err) {
            if (typeof ToastManager !== 'undefined') {
                ToastManager.show('Huddle', "Couldn't access your camera/microphone: " + err.message, 'danger', true);
            }
            throw err;
        }
    }

    async function start(otherUserId, otherUserName) {
        if (state !== 'idle') return;
        ensureDom();
        if (!modal) return;
        const convEl = document.querySelector('#chat-messages-container > div[data-conversation-id]');
        conversationId = convEl && convEl.dataset.conversationId;
        if (!conversationId) return;
        peerName = otherUserName;
        callId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;

        let stream;
        try {
            stream = await getMedia();
        } catch (e) {
            return;
        }
        localStream = stream;

        state = 'ringing_out';
        statusEl.textContent = `Calling ${peerName}...`;
        localVideo.srcObject = localStream;
        localVideo.style.display = 'block';
        activeActions.style.display = 'block';
        modal.show();

        pc = createPeerConnection();
        localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        // ponytail: fold the offer SDP into the invite instead of a separate
        // huddle_offer round trip — one message does both jobs.
        send('huddle_invite', { sdp: offer });
    }

    function handleSignal(data) {
        if (data.type === 'huddle_invite') return onInvite(data);
        if (data.type === 'huddle_answer') return onAnswer(data);
        if (data.type === 'huddle_ice') return onIce(data);
        if (data.type === 'huddle_decline') return onRemoteEnd('declined');
        if (data.type === 'huddle_cancel') return onCancel(data);
        if (data.type === 'huddle_end') return onRemoteEnd('ended');
    }

    function onInvite(data) {
        if (state !== 'idle') {
            // Already on a call / already ringing someone else — busy.
            if (window.d8Socket) {
                window.d8Socket.send(JSON.stringify({
                    type: 'huddle_decline',
                    call_id: data.call_id,
                    conversation_id: data.conversation_id,
                }));
            }
            return;
        }
        ensureDom();
        if (!modal) return;
        callId = data.call_id;
        peerName = null;
        conversationId = data.conversation_id;
        pendingOffer = data.sdp;
        state = 'ringing_in';
        statusEl.textContent = 'Incoming huddle...';
        incomingActions.style.display = 'block';
        modal.show();
    }

    async function accept() {
        if (state !== 'ringing_in' || !pendingOffer) return;
        let stream;
        try {
            stream = await getMedia();
        } catch (e) {
            decline();
            return;
        }
        localStream = stream;
        incomingActions.style.display = 'none';
        activeActions.style.display = 'block';
        localVideo.srcObject = localStream;
        localVideo.style.display = 'block';
        state = 'connecting';
        statusEl.textContent = 'Connecting...';

        pc = createPeerConnection();
        localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
        await pc.setRemoteDescription(new RTCSessionDescription(pendingOffer));
        pendingOffer = null;
        for (const c of pendingCandidates) {
            await pc.addIceCandidate(new RTCIceCandidate(c));
        }
        pendingCandidates = [];
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        send('huddle_answer', { sdp: answer });
    }

    function decline() {
        if (state === 'ringing_in') send('huddle_decline');
        reset();
    }

    function hangup() {
        if (state === 'ringing_out') send('huddle_cancel');
        else if (state !== 'idle') send('huddle_end');
        reset();
    }

    async function onAnswer(data) {
        if (state !== 'ringing_out' || data.call_id !== callId || !pc) return;
        state = 'connecting';
        statusEl.textContent = 'Connecting...';
        await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
        for (const c of pendingCandidates) {
            await pc.addIceCandidate(new RTCIceCandidate(c));
        }
        pendingCandidates = [];
    }

    async function onIce(data) {
        if (data.call_id !== callId || !data.candidate) return;
        if (!pc || !pc.remoteDescription) {
            pendingCandidates.push(data.candidate);
            return;
        }
        try {
            await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
        } catch (e) {
            // Stale candidate for a call that already moved on — ignore.
        }
    }

    function onCancel(data) {
        // Either another of my own tabs answered this call, or the caller
        // cancelled before pickup — if I'm still just ringing, stand down.
        if (data.call_id === callId && (state === 'ringing_in' || state === 'ringing_out')) {
            reset();
        }
    }

    function onRemoteEnd(reason) {
        if (state === 'idle') return;
        if (statusEl) statusEl.textContent = reason === 'declined' ? 'Call declined' : 'Call ended';
        setTimeout(reset, 1200);
    }

    function toggleMic() {
        if (!localStream) return;
        localStream.getAudioTracks().forEach((t) => { t.enabled = !t.enabled; });
        micBtn.classList.toggle('btn-secondary');
        micBtn.classList.toggle('btn-danger');
    }

    function toggleCamera() {
        if (!localStream) return;
        localStream.getVideoTracks().forEach((t) => { t.enabled = !t.enabled; });
        camBtn.classList.toggle('btn-secondary');
        camBtn.classList.toggle('btn-danger');
    }

    return { start, accept, decline, hangup, toggleMic, toggleCamera, handleSignal };
})();

window.Huddle = Huddle;
