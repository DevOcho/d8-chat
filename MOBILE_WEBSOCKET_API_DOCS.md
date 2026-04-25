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
  "mention_count": 1
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
      "mime_type": "application/pdf"
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
  }
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
  "mime_type": "application/pdf"
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

Marks all messages in a conversation as read for the calling user. Call this when the user opens a conversation or scrolls to the bottom of the message list. Broadcasts an `unread_updated` event to all of the user's other connected sessions (web, other mobile devices) so badges clear everywhere simultaneously.

**POST** `/api/v1/conversations/<conversation_id_str>/read`
**Headers:** `Authorization: Bearer <api_token>`

**No request body.**

**Response (204 No Content):** No body. On success, the server sends the following event to the user's other sessions via WebSocket:

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
- Idempotent — calling on an already-read conversation is a no-op.
- The calling session does not receive the `unread_updated` echo; only other sessions do.

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
* `around_message_id` (optional, int): Pass a specific message ID to fetch a contextual window of messages (15 before, the target message itself, and 15 after). Useful for "jump to message" from search results.

**Response (200 OK):**
```json
{
  "messages": [
    { ...Message Object... },
    { ...Message Object... }
  ]
}
```

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
Received when a message is deleted.
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
Received when a message is posted in a conversation the user is *not* actively subscribed to. Use this to update the unread counters/badges in the sidebar UI.
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
