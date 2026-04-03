# D8-Chat Mobile WebSocket API Documentation

This document outlines the JSON-based WebSocket API designed specifically for the D8-Chat mobile application. Unlike the web client (which receives rendered HTML via HTMX), the mobile endpoint consumes and emits strictly structured JSON.

## Connection & Authentication

**Endpoint:** `wss://<your-domain.com>/api/v1/ws?token=<api_token>`

To connect to the WebSocket, the mobile client must pass a valid API token in the query string.
* The token can optionally include the `d8_sec_` prefix (the server will automatically parse it).
* If the token is missing, invalid, or expired, the server will close the connection immediately with **Close Code 1008** and the message `"Invalid or missing token"`.

---

## Data Models

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
      "url": "https://<minio-url>/file.pdf",
      "original_filename": "file.pdf",
      "mime_type": "application/pdf"
    }
  ],
  "thread_reply_count": 0,
  "last_reply_at": null,
  "poll": null // or Poll Object if applicable
}
```

---

## Client-to-Server Events (Sending)

The mobile app should send stringified JSON objects to the server to perform actions.

### 1. Subscribe to a Conversation
Tells the server which conversation the user is actively viewing. This prevents push notifications for this specific channel while the user is looking at it, and marks incoming messages as read.
```json
{
  "type": "subscribe",
  "conversation_id": "channel_5" // or "dm_1_2"
}
```

### 2. Send a Message
Posts a new message to the currently subscribed conversation.
```json
{
  "type": "send_message",
  "conversation_id": "channel_5",
  "content": "This is my message!",

  // Optional Fields:
  "parent_message_id": 42,           // Included if replying in a thread or quoting
  "reply_type": "thread",            // "thread" or "quote"
  "quoted_message_id": 42,           // Included if quoting
  "attachment_file_ids": "12,13"     // Comma-separated string of pre-uploaded file IDs
}
```

### 3. Typing Indicators
Broadcasts to other users that the current user is typing. You should debounce this on the client side (e.g., send `typing_start`, wait 1.5 seconds after the user stops typing, then send `typing_stop`).
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

## Server-to-Client Events (Receiving)

The mobile app must listen for the following JSON payloads emitted by the server.

### 1. New Standard Message
Received when a new message is posted in a conversation the user is a part of.
```json
{
  "type": "new_message",
  "data": { ...Message Object... }
}
```

### 2. New Thread Reply
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

### 3. Unread Counts / Badges Update
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

### 4. User Presence Update
Received globally when any user goes online, offline (away), or sets their status to busy. Use the `status` string to evaluate presence natively.
```json
{
  "type": "presence_update",
  "user_id": 1,
  "status_class": "presence-online", // Retained for web backward compatibility
  "status": "online"                 // "online", "away", or "busy"
}
```

### 5. Typing Update
Received when the typing status of a conversation changes. Provides a list of usernames currently typing in that conversation.
```json
{
  "type": "typing_update",
  "conversation_id": "channel_5",
  "typists": ["jdoe", "jsmith"]
}
```

### 6. User Avatar Update
Received globally when a user changes their profile picture.
```json
{
  "type": "avatar_update",
  "user_id": 1,
  "avatar_url": "https://<minio-url>/new_avatar.png"
}
```

### 7. Push Notification Triggers (System Events)
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

### 8. Direct Message Created
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

### 9. Message Edited
Received when an existing message in a subscribed channel is edited. The payload contains the fully updated Message object.
```json
{
  "type": "message_edited",
  "data": { ...Message Object... }
}
```

### 10. Message Deleted
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
### 11. Reaction Updated
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
