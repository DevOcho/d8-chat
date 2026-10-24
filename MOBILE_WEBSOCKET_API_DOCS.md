# D8-Chat Mobile API & WebSocket Documentation

This document outlines the JSON-based API and WebSocket events designed specifically for the D8-Chat mobile application.

**Architectural Note:**
We use **REST (HTTP)** for mutations (sending messages) to utilize standard HTTP status codes and ease retry logic, and **WebSockets** exclusively for real-time subscriptions (receiving events).

---

## 1. Connection & Authentication

**WebSocket Endpoint:** `wss://<your-domain.com>/ws/api/v1`

The API token is sent via the `Sec-WebSocket-Protocol` upgrade header — **never** as a URL query parameter, since query strings leak into reverse-proxy access logs, browser history, and Referer headers.

The client must request **two** subprotocols, comma-separated:

1. `d8_sec` — the marker telling the server this is a D8-Chat authenticated connection.
2. The API token itself. The token *may* include the `d8_sec_` prefix (e.g. `d8_sec_abc123…`) or be sent without it.

The server completes the WebSocket handshake by echoing back `Sec-WebSocket-Protocol: d8_sec`.

### Examples

**JavaScript (browser / React Native):**
```js
const token = "d8_sec_abc123…"; // from POST /api/v1/auth/login
const ws = new WebSocket("wss://example.com/ws/api/v1", ["d8_sec", token]);
```

**Swift (iOS, URLSessionWebSocketTask):**
```swift
var request = URLRequest(url: URL(string: "wss://example.com/ws/api/v1")!)
request.setValue("d8_sec, \(token)", forHTTPHeaderField: "Sec-WebSocket-Protocol")
let task = URLSession.shared.webSocketTask(with: request)
task.resume()
```

**Kotlin (Android, OkHttp):**
```kotlin
val request = Request.Builder()
    .url("wss://example.com/ws/api/v1")
    .header("Sec-WebSocket-Protocol", "d8_sec, $token")
    .build()
val ws = client.newWebSocket(request, listener)
```

### Failure modes
* If the `Sec-WebSocket-Protocol` header is missing, doesn't include a token, or the token is invalid/expired, the server closes the connection immediately with **Close Code 1008** and message `"Invalid or missing token"`.
* The server will not echo back `d8_sec` if the client did not offer it; well-behaved clients will treat the missing echo as a handshake failure and disconnect.

> **Migration note:** Earlier drafts of this document showed `?token=…` in the URL. That mechanism has been removed and is no longer accepted. Update any client built against the older spec.

### Keepalive, reconnect, and delivery guarantees

* **Server pings.** The server sends a WebSocket PING control frame every ~25s.
  Keep your client library's automatic PONG enabled (it is by default) — an idle
  socket that stops answering pings is torn down so a dead connection doesn't
  linger half-open. Application-level pings are optional: protocol PONGs are
  invisible to most app code, so if you want to detect a half-open socket
  yourself (e.g. after the app resumes), send `{"type":"ping"}` and expect
  `{"type":"pong"}` back (see *Heartbeat*).
* **Reconnect.** On an unexpected close, reconnect with jittered exponential
  backoff (e.g. capped at ~30s). Do **not** reconnect on close code **1008**
  (auth rejected) — refresh the token first. After reconnecting you must
  **re-subscribe** to the active conversation (`{"type":"subscribe", ...}`).
* **Delivery is at-most-once; fill gaps yourself.** Redis pub/sub does not
  replay. Any event broadcast while you were disconnected is **not** re-sent on
  reconnect. After reconnecting, re-fetch history with
  `GET /api/v1/conversations/<id>/messages` (see *Fetch Message History*) using
  the id of the last message you already have, and reconcile. Treat realtime
  events as a fast path, not a guaranteed log.
* **Presence lag.** Cluster presence decays on a heartbeat TTL, so a user who
  disconnects may briefly still read as online (up to ~90s) before aging out.

---

## 2. Data Models

Many of the events below utilize a standardized `Message` and `User` object payload.

### `User` Object
```json
{
  "id": 1,
  "username": "jdoe",
  "email": "jdoe@example.com",
  "display_name": "John Doe",
  "avatar_url": "https://<minio-url>/avatar.png",
  "presence_status": "online" // "online", "away", or "busy"
}
```

### `Channel` object
```json
{
  "id": 5,
  "name": "project-alpha",
  "topic": "Discussions about Project Alpha",
  "description": "Main channel for the Alpha team.",
  "is_private": true,
  "unread_count": 3,
  "mention_count": 1,
  "last_message": {               // null when the channel has no messages
    "id": 812,
    "content_preview": "Deploy is done 🎉", // whitespace collapsed, emoji shortcodes expanded, ≤140 chars
    "user": { ...User Object... },   // null for a system message
    "created_at": "2026-10-24T15:02:11.123456",
    "has_attachments": false
  },
  "last_activity_at": "2026-10-24T15:02:11.123456" // last_message.created_at, or null
}
```

### `Message` Object
```json
{
  "id": 42,
  "conversation_id_str": "channel_5",
  "content": "Hello team! Check out this new feature.",
  "created_at": "2024-04-03T09:30:00.123456",
  "is_edited": false,
  "is_deleted": false,
  "user": { ...User Object... },
  "reply_type": null, // null, "quote", or "thread"
  "parent_message_id": null, // int or null
  "quoted_message_id": null, // int or null
  "quoted_message": {
    "id": 100,
    "content": "The original message text",
    "user": {
      "id": 2,
      "username": "admin",
      "email": "admin@example.com",
      "display_name": "Admin User",
      "avatar_url": "https://...",
      "presence_status": "away"
    }
  },
  "reactions": [
    {
      "emoji": "👍",
      "count": 2,
      "users": [1, 3],
      "reactor_names": ["John Doe", "Jane Smith"]
    }
  ],
  "attachments": [
    {
      "file_id": 42,
      "url": "https://<minio-url>/file.pdf",
      "original_filename": "file.pdf",
      "mime_type": "application/pdf",
      "width": null,  // pixels, for images; null for other files and older images
      "height": null
    }
  ],
  "thread_reply_count": 0,
  "last_reply_at": null,
  "poll": {
    "id": 1,
    "question": "Where should we go for lunch?",
    "voted_option_id": 4, // null if the current user has not voted
    "options": [
      {
        "id": 4,
        "text": "Tacos",
        "count": 3
      },
      {
        "id": 5,
        "text": "Pizza",
        "count": 1
      }
    ]
  },
  "incident": null // Incident Object on an Uptime Kuma DOWN message that opened one (see §6)
}
```

---

## 3. REST API

### Get App Configuration
Returns server configuration and SSO details for the mobile app launch screen. No authentication required.

**GET** `/api/v1/app-config`

**Response (200 OK):**
```json
{
  "server_name": "DevOcho",
  "logo_url": null,
  "primary_color": "#ec729c",
  "password_auth_enabled": true,
  "sso_enabled": true,
  "sso_provider_name": "Sign in with SSO",
  "sso_auth_url": "https://...",
  "version": "1.0.0"
}
```

### SSO Token Exchange
Exchanges an OIDC authorization code for a d8-chat API token. No authentication required.

**POST** /api/v1/auth/sso/exchange
**Headers:** Content-Type: application/json

Request Body:
```json
{
  "code": "<authorization_code>",
  "redirect_uri": "d8chat://auth/callback"
}
```

Response (200 OK):
Returns the api_token and serialized User object.
```json
{
  "api_token": "d8_sec_...",
  "user": { ...User Object... }
}
```

### Messages
Use this endpoint to send new messages. This automatically broadcasts the real-time WebSocket events to all other active clients.

**POST** `/api/v1/conversations/<conversation_id_str>/messages`
**Headers:** `Authorization: Bearer <api_token>`

**Request Body:**
```json
{
  "content": "This is my message!",

  // Optional Fields:
  "parent_message_id": 42,           // Included if replying in a thread or quoting
  "reply_type": "thread",            // "thread" or "quote"
  "quoted_message_id": 42,           // Included if quoting
  "attachment_file_ids": "12,13"     // Comma-separated string of pre-uploaded file IDs
}
```

**Response (201 Created):**
Returns the fully serialized `Message` Object.

### Upload a File
Use this endpoint to pre-upload a file before attaching it to a new message. The returned `file_id` should be passed into the `attachment_file_ids` string when sending the message.

**POST** `/api/v1/files/upload`
**Headers:**
* `Authorization: Bearer <api_token>`
* `Content-Type: multipart/form-data`

**Request Body:**
* `file`: The binary file data.

**Response (201 Created):**
```json
{
  "file_id": 42,
  "message": "File uploaded successfully",
  "url": "https://<minio-url>/...",
  "original_filename": "document.pdf",
  "mime_type": "application/pdf",
  "width": null,   // pixels for an image (as stored, after server-side resizing)
  "height": null
}
```

### Download/View a File
Use this endpoint to securely access a file's content. This endpoint authenticates your request and streams the binary file data directly to you. It includes a Cache-Control header, so please ensure your networking library respects it to prevent re-downloading images unnecessarily.

**GET** `/api/v1/files/<file_id>/content`
**Headers:**
* `Authorization: Bearer <api_token>`

**Response (200 OK):**
Binary file stream with the appropriate `Content-Type` and `Content-Disposition` headers.

### Fetch Conversation Members
Returns a list of `User` objects that are participants in a specific conversation. Useful for `@mention` autocomplete interfaces.

**GET** `/api/v1/conversations/<conversation_id_str>/members`
**Headers:** `Authorization: Bearer <api_token>`

**Response (200 OK):**
```json
{
  "members": [
    { ...User Object... },
    { ...User Object... }
  ]
}
```

### Mark Conversation as Read

Marks all messages in a conversation as read for the calling user. Call this when the user opens a conversation or scrolls to the bottom of the message list. The server clears its badge on all of the user's connected clients: web tabs update their sidebar, and mobile sockets get an `unread_updated` event with a zero count. Opening an unread conversation on the web does the same, so reading on any client clears the badge everywhere.

**POST** `/api/v1/conversations/<conversation_id_str>/read`
**Headers:** `Authorization: Bearer <api_token>`

**No request body.**

**Response (204 No Content):** No body. The server sends the following event to the user's mobile sockets:

```json
{
  "type": "unread_updated",
  "data": {
    "conversation_id_str": "channel_5",
    "unread_count": 0,
    "is_mention": false
  }
}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| `403` | User is not a member of the conversation |
| `404` | Conversation ID not found |

**Notes:**
- Idempotent: calling it on an already-read conversation changes nothing, but the clear is still re-sent, which heals a badge another client missed.
- The caller's own socket receives the event too. Its count is zero, so applying it is harmless.

---

### Create a Poll
Creates a new message containing a poll. Broadcasts the standard new_message WebSocket event to all other clients upon success.

**POST** `/api/v1/conversations/<conversation_id_str>/polls`
**Headers:** `Authorization: Bearer <api_token>`

**Request Body:**
```json
{
  "question": "What's for lunch?",
  "options": ["Pizza", "Tacos", "Salad"]
}
```

**Response (201 Created):**
Returns the fully serialized Message Object (including the new poll dictionary).

### Vote on a Poll
Casts, changes, or removes a vote on a poll option. If the user selects the same option they already voted for, their vote is removed. If they select a new option, their vote is switched. This automatically triggers a message_edited WebSocket event for all clients.

**POST** `/api/v1/polls/<poll_id>/vote`
**Headers:** `Authorization: Bearer <api_token>`

Request Body:
```json
{
  "option_id": 4
}
```

**Response (200 OK):**
Returns the updated Message Object.

### Fetch Message History
Returns a paginated list of messages for a given conversation. By default, it returns the 30 most recent messages. You can use query parameters to paginate backwards or to jump to a specific context window.

**GET** `/api/v1/conversations/<conversation_id_str>/messages`
**Headers:** `Authorization: Bearer <api_token>`

**Query Parameters:**
* `before_message_id` (optional, int): Pass the oldest message ID you currently have to fetch the next page of older history.
* `after_message_id` (optional, int): Pass the newest message ID you have to fetch the next 30 newer messages, oldest first. Use it to fill a gap after reconnecting, or to scroll forward after a jump; repeat until fewer than 30 come back.
* `around_message_id` (optional, int): Pass a specific message ID to fetch a contextual window of messages (15 before, the target message itself, and 15 after). Useful for "jump to message" from search results. The target must belong to this conversation.

Pass at most one of the three (400 otherwise). Thread replies are never included; fetch them with the thread endpoint.

**Response (200 OK):**
```json
{
  "messages": [
    { ...Message Object... },
    { ...Message Object... }
  ]
}
```

### Conversation Lists (home screen)
**GET** `/api/v1/channels` returns `{"channels": [ ...Channel Object... ]}`, sorted by name.
**GET** `/api/v1/dms` returns `{"dms": [...]}`, most recent activity first:
```json
{
  "conversation_id_str": "dm_3_7",
  "other_user": { ...User Object... },
  "unread_count": 2,
  "last_message": { ... },          // same shape as on the Channel Object
  "last_activity_at": "2026-10-24T15:02:11.123456"
}
```
`last_message` is the newest message in the main timeline (thread replies excluded).

### Edit a Message / Toggle a Reaction
**PATCH** `/api/v1/messages/<id>` with `{"content": "..."}` returns the updated `Message` Object (200).
**POST** `/api/v1/messages/<id>/reactions` with `{"emoji": "👍"}` toggles your reaction and returns:
```json
{
  "message_id": 42,
  "conversation_id_str": "channel_5",
  "reactions": [ ... ],
  "message": { ...Message Object... }   // the updated message
}
```
Both responses carry the new state, so the app doesn't need to wait for the
`message_edited` / `reaction_updated` WebSocket echo.

### Global Search
Searches across messages, channels, and people within the authenticated user's workspace. Results are scoped to what the user is permitted to see.

**GET** /api/v1/search
**Headers:** Authorization: Bearer <api_token>

**Query Parameters:**
q (required, string): Search query string (min 2 chars).
limit (optional, int): Max results per bucket. Defaults to 20, max 50.

Response (200 OK):
```json
{
  "query": "design",
  "messages": [
    {
      "id": 456,
      "content": "Has anyone reviewed the new design system docs?",
      "created_at": "2026-04-02T14:30:00.123456",
      "conversation_id_str": "channel_3",
      "conversation_name": "general",
      "user": { ...User Object... }
    }
  ],
  "channels": [
    {
      "id": 7,
      "name": "design",
      "description": "Design team discussion",
      "is_private": false,
      "conv_id": "channel_7",
      "member_count": 5
    }
  ],
  "people": [
    {
      "id": 4,
      "username": "designlead",
      "display_name": "Design Lead",
      "avatar_url": null,
      "presence_status": "online",
      "dm_conv_id": "dm_1_4"
    }
  ]
}
```
Note on Search People: `dm_conv_id` provides the existing DM conversation ID between the current user and the found person. If null, no DM exists yet, and the app should call the standard DM creation flow when tapped.

### Update Profile
Updates the authenticated user's display name.

**PATCH** `/api/v1/users/me`
**Headers:**
* `Authorization: Bearer <api_token>`
* `Content-Type: application/json`

**Request Body:**
```json
{
  "display_name": "New Name"
}
```
Response (200 OK): Returns the updated User object.

### Update Avatar
Uploads and sets a new avatar for the authenticated user. Automatically broadcasts the change to active clients.

**POST** /api/v1/users/me/avatar
**Headers:**
Authorization: Bearer <api_token>
Content-Type: multipart/form-data

Request Body:
file: The binary image data.

Response (200 OK):
```json
{
  "avatar_url": "https://<minio-url>/..."
}
```

### Update Presence
Updates the user's presence status and broadcasts the change to all connected clients. Valid values: "online", "away", "busy".

**POST** /api/v1/users/me/presence
**Headers:**
Authorization: Bearer <api_token>
Content-Type: application/json

Request Body:
```json
{
  "status": "busy"
}
```

Response (200 OK):
```json
{
  "status": "busy"
}
```

### Register a device for push notifications
`POST /api/v1/users/me/devices`

Call after login and whenever the OS rotates the FCM token. The endpoint upserts
on the token string: re-registering the same token under the same user is a
refresh, and re-registering it under a new user (shared/reprovisioned device)
reassigns the existing row. Rate-limited to 30 requests/minute per user.

Request Body:
```json
{
  "platform": "android",
  "token": "<fcm registration token>"
}
```

`platform` must be `"android"` or `"ios"`. `token` is the FCM registration
token (capped at 4096 chars; real tokens are far shorter).

Response: `204 No Content` on success. `400` on missing/invalid fields,
`401` without a valid Bearer token, `429` if rate-limited.

iOS pushes ride on the FCM APNs bridge — your client still registers an FCM
token, not an APNs device token. The backend doesn't need a code change to
light up iOS; the operator just uploads the project's `.p8` APNs key in
Firebase project settings.

### Deregister a device
`DELETE /api/v1/users/me/devices`

Call on logout (and any time the app wants to permanently stop pushes to
this device). Idempotent — returns 204 whether or not the token existed.
Only deletes tokens that belong to the calling user.

Request Body:
```json
{
  "token": "<fcm registration token>"
}
```

Response: `204 No Content`. `400` on missing `token`.

### Push notification payload

When a push lands on the device, FCM delivers the notification block plus
this `data` payload (all values are stringified by FCM in transit):

```json
{
  "conversation_id_str": "dm_1_42",   // or "channel_5"
  "message_id": "12345",
  "parent_message_id": "12200"        // only present for thread replies
}
```

Use these to deep-link from the notification tap straight to the conversation
(or to the thread modal). Pushes fire for:
 - DMs to the user
 - Direct `@username` mentions of the user
 - Thread replies on threads the user has participated in (or started)

`@channel` / `@here` do **not** trigger a push in v1 — too noisy on mobile.

---

## 4. Client-to-Server WS Events (Sending)

The mobile app should send stringified JSON objects over the WebSocket to perform real-time interactions.

### Subscribe to a Conversation
Tells the server which conversation the user is actively viewing. This prevents push notifications for this specific channel while the user is looking at it, and marks incoming messages as read.
```json
{
  "type": "subscribe",
  "conversation_id": "channel_5" // or "dm_1_2"
}
```

### Typing Indicators
Broadcasts to other users that the current user is typing. Debounce this on the client side (e.g., send `typing_start`, wait 1.5 seconds after the user stops typing, then send `typing_stop`).
```json
{
  "type": "typing_start",
  "conversation_id": "channel_5"
}
```
```json
{
  "type": "typing_stop",
  "conversation_id": "channel_5"
}
```

### Heartbeat
Optional. The server answers on the same socket with `{"type": "pong"}`. Any
frame arriving after your ping proves the socket is alive; if nothing arrives
within ~10s, close it and reconnect (then re-subscribe and fill the gap).
```json
{
  "type": "ping"
}
```

---

## 5. Server-to-Client WS Events (Receiving)

The mobile app must listen for the following JSON payloads emitted by the server.

### New Standard Message
Received when a new message is posted in a conversation the user is a part of.
```json
{
  "type": "new_message",
  "data": { ...Message Object... }
}
```

### New Thread Reply
Received when someone replies inside a thread. Includes both the updated parent message and the new reply.
```json
{
  "type": "new_thread_reply",
  "data": {
    "parent_message": { ...Message Object... },
    "reply": { ...Message Object... }
  }
}
```

### Message Edited
Received when an existing message is edited. The payload contains the fully updated Message object.
```json
{
  "type": "message_edited",
  "data": { ...Message Object... }
}
```

### Message Deleted
Received when a message is deleted. A message that starts a thread is not
removed: it arrives as `message_edited` with `"is_deleted": true` and empty
`content`, and should render as a "This message was deleted." placeholder that
still opens its thread. When its last reply is deleted, the placeholder gets a
`message_deleted` of its own.
```json
{
  "type": "message_deleted",
  "data": {
    "message_id": 42,
    "conversation_id_str": "channel_5"
  }
}
```

### Reaction Updated
Received when a reaction is added or removed. Completely replaces the reactions array for that specific message.
```json
{
  "type": "reaction_updated",
  "data": {
    "message_id": 42,
    "conversation_id_str": "channel_5",
    "reactions": [
      {
        "emoji": "👍",
        "count": 2,
        "users": [1, 3],
        "reactor_names": ["John Doe", "Jane Smith"]
      }
    ]
  }
}
```

### Direct Message Created
Received when another user starts a new direct message conversation with the current user. Use this to dynamically add the DM to the sidebar.
```json
{
  "type": "dm_created",
  "data": {
    "conversation_id_str": "dm_1_2",
    "other_user": { ...User Object... }
  }
}
```

### Unread Counts / Badges Update
Received when a message is posted in a conversation the user is *not* actively subscribed to. Use this to update the unread counters/badges in the sidebar UI. Also received with `unread_count: 0` when the user reads the conversation on any client (web or mobile): clear both the unread and mention badges.
```json
{
  "type": "unread_updated",
  "data": {
    "conversation_id_str": "channel_5",
    "unread_count": 1,
    "is_mention": true
  }
}
```

### User Presence Update
Received globally when any user goes online, offline (away), or sets their status to busy. Use the `status` string to evaluate presence natively.
```json
{
  "type": "presence_update",
  "user_id": 1,
  "status_class": "presence-online", // Retained for web backward compatibility
  "status": "online"                 // "online", "away", or "busy"
}
```

### Typing Update
Received when the typing status of a conversation changes. Provides a list of usernames currently typing in that conversation.
```json
{
  "type": "typing_update",
  "conversation_id": "channel_5",
  "typists": ["jdoe", "jsmith"]
}
```

### User Avatar Update
Received globally when a user changes their profile picture.
```json
{
  "type": "avatar_update",
  "user_id": 1,
  "avatar_url": "https://<minio-url>/new_avatar.png"
}
```

### Push Notification Triggers (System Events)
When a message triggers a notification (e.g., a direct mention or a DM while the user is looking away), the server emits generic system events. The mobile app can use these to trigger local device vibrations or push-style banner alerts.
```json
{
  "type": "sound"
}
```
```json
{
  "type": "notification",
  "title": "New mention from jdoe",
  "body": "Hello @jsmith, can you check this?",
  "icon": "/favicon.ico",
  "tag": "channel_5"
}
```

---

## 6. On-Call Paging

Uptime Kuma posts monitor events into `#devops-uptime`. A **DOWN** event opens
an *incident* and **pages** the on-call engineer. If nobody acknowledges, it
escalates every 10 minutes (team lead → director → CEO). Pages are cumulative:
everyone paged so far keeps being paged until someone **acknowledges**. The
monitor's **UP** event resolves the incident automatically.

The phone is the real pager, so the app should alert as loudly as the OS
allows, keep alerting until the page is acknowledged or cleared, and let the
user acknowledge from the alert.

### `Incident` Object
```json
{
  "id": 17,
  "title": "Acme Portal",                      // monitor name
  "details": "connect ECONNREFUSED 10.0.0.5:443",
  "source": "uptime-kuma",
  "source_url": "https://portal.acme.example",
  "state": "triggered",                         // "triggered" | "acknowledged" | "resolved"
  "current_level": 2,                           // last escalation level paged
  "triggered_at": "2026-10-10T16:00:52.784415", // naive UTC, like every API timestamp
  "next_escalation_at": "2026-10-10T16:11:55.713491", // null at the last level / once acknowledged
  "acknowledged_at": null,
  "acknowledged_by": null,                      // { "id", "username", "display_name" }
  "resolved_at": null,
  "resolved_by": null,                          // null + resolved_at set = resolved automatically
  "channel_id": 4,
  "channel_name": "devops-uptime",
  "conversation_id_str": "channel_4",
  "message_id": 123,                            // the DOWN message (deep link target)
  "paged_users": [
    { "id": 3, "username": "engineer", "display_name": "DevOps Engineer", "level": 1 },
    { "id": 4, "username": "robert", "display_name": "Robert", "level": 2 }
  ]
}
```

### My on-call status
`GET /api/v1/oncall/me`

```json
{
  "on_call": { "schedule_id": 1, "schedule_name": "DevOps Primary", "until": "2026-10-12T12:00:00" },
  "upcoming_shifts": [
    {
      "schedule_id": 1,
      "schedule_name": "DevOps Primary",
      "start": "2026-10-19T12:00:00",
      "end": "2026-10-26T12:00:00",
      "is_rotation_turn": true,   // false = only covering via an override
      "overrides": [ { "id": 3, "user_id": 5, "start": "...", "end": "...", "reason": "Vacation" } ]
    }
  ],
  "active_pages": [ ...Incident Objects paging me... ]
}
```
`on_call` is `null` when the caller isn't on call right now. `upcoming_shifts`
covers the next 8 weeks. Overrides are listed per shift; a shift can contain
partial overrides.

### Active pages
`GET /api/v1/oncall/incidents/active` → `{ "incidents": [ ...Incident Objects... ] }`

Incidents currently paging the caller (state `triggered`). **Call this on app
launch, on foreground, and after every WebSocket reconnect.** Pub/sub delivery
is at-most-once, so this is how a missed `page` frame is recovered.

### Incident detail
`GET /api/v1/oncall/incidents/<id>` → `{ "incident": { ...Incident Object..., "events": [...] } }`

`events` is the timeline: `{ "kind", "at", "user_id", "details" }` where
`kind` is one of `triggered`, `paged`, `escalated`, `level_skipped`,
`no_oncall`, `fallback_admins`, `repeat_down`, `acknowledged`, `resolved`.
Returns `403` unless the caller is a member of the incident's channel or in its
escalation chain.

### Acknowledge
`POST /api/v1/oncall/incidents/<id>/ack` → `200 { "incident": {...} }`

Stops paging and escalation **for everyone**. Allowed for anyone in the
escalation chain (rotation members, the lead/director/CEO groups, anyone
already paged, workspace admins). `403` outside the chain. `409` with the
current `incident` if it's no longer `triggered` (someone acknowledged first,
or it resolved). Treat that as success and stop alerting.

### Resolve
`POST /api/v1/oncall/incidents/<id>/resolve` → `200 { "incident": {...} }`

Manual resolve, which is rarely needed because the UP event resolves
automatically. Same permissions as acknowledge. `409` if it's already resolved.

### Test my pager
`POST /api/v1/oncall/test-page` → `204`

Rings the caller's own pager: a `page` WS frame with `"test": true` on every
socket, plus a page push to every registered device. No incident is created
and nobody else is paged, so acknowledging a test page is purely local (don't
call `/ack`, since its `id` is a string, not an incident). Rate limited to 6 per minute.

### WS: `page`
Sent to each paged user when they're paged, and **re-sent every ~60s** while
the incident stays `triggered`. `repeat: true` marks a re-send. If the
incident is already showing, just make sure the alert is still going; don't
stack a second alert.
```json
{
  "type": "page",
  "repeat": false,
  "test": false,            // true for "Test my pager" (no incident exists; "id" is a string)
  "incident": { ...Incident Object... }
}
```

### WS: `page_cleared`
The page stopped: it was acknowledged by someone, or resolved. Stop the alarm and
dismiss the alert for `incident_id`.
```json
{ "type": "page_cleared", "incident_id": 17, "state": "acknowledged", "incident": { ... } }
```

### Incident card on messages
Every Message Object has an `incident` key. On the Uptime Kuma DOWN message
that opened an incident, it is the Incident Object, so draw the card from it.
On every other message it is `null`. It is set wherever messages are
serialized: history (including `?around_message_id=`), threads, and the live
`new_message` frame for the DOWN message itself. It reflects the incident's
state when the message was fetched, so keep it current with `incident_update`.

### WS: `incident_update`
Broadcast to everyone subscribed to the incident's channel whenever its state
changes. Use it to refresh the incident card under the DOWN message
(`incident.message_id`).
```json
{ "type": "incident_update", "data": { ...Incident Object... } }
```

### Push: page
Pages go to **every** registered device of each paged user. Unlike chat pushes,
they are **not** suppressed when the user is active on the web. They are re-sent
about every 60s until the page is acknowledged or cleared.

- `notification`: `title` `"🚨 DOWN: <monitor>"`, `body` = details.
- `data`: `{ "type": "page", "incident_id": "17", "conversation_id_str": "channel_4", "message_id": "123" }`
  (a test page has `"test": "1"` and a string `incident_id`).
- **Lifetime:** a page that can't be delivered expires after **120 s** (Android
  `ttl`, iOS `apns-expiration`). A phone that was off or offline won't ring for
  a page that ended long ago when it reconnects. Active pages repeat about
  every 60 s, so this loses nothing.
- **Android:** high-priority message with `ttl: 120s` and `collapse_key: incident-<id>`
  (an offline phone gets only the newest page per incident). Notification
  `channel_id` **`oncall_pages`**, priority `max`, tag `incident-<id>`, and
  visibility **`public`**, so the monitor name shows on lock screens that hide
  sensitive content. Create the `oncall_pages` channel with `IMPORTANCE_HIGH`, an alarm sound, and vibration,
  and consider a full-screen intent. Repeats reuse the tag, so they replace the
  notification and alert again instead of stacking.
- **iOS:** `apns-priority: 10`, `apns-collapse-id: incident-<id>`,
  `apns-expiration` 120 s out, and a **critical** alert (`interruption-level:
  critical`, critical sound **`pager.caf`** at volume 1.0, category
  **`ONCALL_PAGE`**). Bundle a `pager.caf` (under 30 s) in the app for the
  alarm sound; without it iOS plays the default alert sound. Register the
  `ONCALL_PAGE` category with an Acknowledge action that calls
  `POST /api/v1/oncall/incidents/<id>/ack`. Until the app registers it, iOS ignores the category. A critical alert plays through silent mode and Do Not Disturb, but
  **only once Apple grants the app the Critical Alerts entitlement**. Request it
  from Apple early, because approval is manual. Without it, iOS delivers the page as a normal alert.

### Push: page cleared
`data`: `{ "type": "page_cleared", "incident_id": "17", "state": "acknowledged" }`
(`state` is `acknowledged` or `resolved`).

- **Android:** data-only, high priority, with no `notification`. Cancel the
  notification tagged `incident-<id>` and stop any in-app alarm.
- **iOS:** a quiet visible alert that **replaces the page in place**:
  `apns-push-type: alert`, `apns-priority: 10`, and the page's own
  `apns-collapse-id: incident-<id>`. The `aps` is `alert` `"✅ <monitor> acknowledged by <name>"`
  (or `"✅ <monitor> resolved"`), `interruption-level: passive`, `thread-id:
  incident-<id>`, no sound, and `content-available: 1`, so a running app still
  gets the data and can stop its alarm. Unlike a background push, this
  reaches a force-quit app, so a page someone else acknowledged doesn't stay
  on the lock screen.
- **No expiration**, unlike the page. A phone that received a page and then
  went offline still gets the clear when it reconnects, so the stale page is
  replaced. The cost is that a phone offline for the whole incident may show a
  quiet ✅ for a page it never saw.
