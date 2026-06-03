# Telegram Bot-to-Bot Communication

Last checked: 2026-05-29

Sources:

- Telegram Bot Features: https://core.telegram.org/bots/features
- Telegram Bot API changelog: https://core.telegram.org/bots/api#may-8-2026
- Telegram Bot API `sendMessage`: https://core.telegram.org/bots/api#sendmessage

## What Changed

Telegram Bot API 10.0 added support for bot-to-bot communication. Bots can now send messages to other bots by username, and bots can interact in groups when they are addressed through commands, mentions, or replies.

This is different from older Telegram bot behavior, where bot-to-bot messaging was generally treated as unsupported or unreliable.

## Supported Interaction Modes

### Private bot-to-bot chat

A bot can send a private message to another bot by using the target bot username as `chat_id`.

Example:

```python
await bot.send_message(
    chat_id="@other_bot",
    text="hello from HeroBot",
)
```

Important constraints:

- Bot-to-bot private messaging must be enabled in BotFather for the relevant bots.
- Use this for direct agent-to-agent calls where the other bot is intended to receive bot-originated messages.
- Expect normal Bot API errors if the target bot does not exist, does not allow this mode, or the username is wrong.

### Group bot-to-bot communication

In a group or supergroup, a bot can address another bot using a command, mention, or reply.

Examples:

```text
/status@other_bot
@other_bot summarize this thread
```

Or a bot can reply to a message originally sent by another bot.

Development notes:

- Group Privacy still matters. If privacy mode is enabled, bots usually only receive commands, mentions, replies, and selected service messages.
- Turning privacy off lets the bot receive more group messages, but HeroBot should still only respond when addressed to avoid noisy group behavior.
- At least one side of bot-to-bot group interaction must be configured to allow the interaction according to Telegram's current bot-to-bot mode rules.

### Business bot-to-bot communication

Telegram also documents bot-to-bot interactions in Business contexts. Treat this as a separate integration surface from regular private chats and groups. HeroBot currently does not implement Business connection behavior.

## API Surface To Use

### Sending to another bot

Use the existing `sendMessage` method. The new part is that `chat_id` may be a bot username.

```python
await context.bot.send_message(
    chat_id="@other_bot",
    text="Task request payload here",
)
```

For group calls, send into the current group and address the target bot in text:

```python
await context.bot.send_message(
    chat_id=group_chat_id,
    text="@other_bot please handle this",
)
```

### Receiving from another bot

Incoming updates still arrive through normal message updates. Detect bot senders with:

```python
user = update.effective_user
if user and user.is_bot:
    ...
```

Useful fields:

- `update.effective_user.id`
- `update.effective_user.username`
- `update.effective_user.is_bot`
- `update.effective_chat.id`
- `update.effective_chat.type`
- `update.effective_message.text`
- `update.effective_message.reply_to_message`

### Group member visibility

Bot-to-bot communication does not mean a bot can list every group member.

Available group APIs:

- `getChatMemberCount`: count members.
- `getChatAdministrators`: list admins only.
- `getChatMember`: query one known user id.

There is no general Bot API method for enumerating all group members.

## Safety Rules For HeroBot

Bot-to-bot systems can loop. For HeroBot development, require these safeguards before enabling automatic bot-to-bot calls:

- Allowlist bot usernames or bot user ids.
- Ignore messages from unknown bots.
- Add a call id to every outbound bot-to-bot request.
- Keep a short-term dedupe cache of processed call ids.
- Add a max depth or hop count.
- Add per-chat and per-bot rate limits.
- Do not let another bot directly trigger privileged personal-data tools unless explicitly allowed.

Suggested `.env` shape:

```env
ENABLE_BOT_TO_BOT=true
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot
BOT_TO_BOT_MAX_DEPTH=3
BOT_TO_BOT_RATE_LIMIT_PER_MINUTE=20
```

Suggested message envelope for plain-text calls:

```text
[herobot-call-id: 01J...]
[herobot-depth: 1/3]
[herobot-from: super666666_bot]

请处理这个任务：...
```

If structured payloads become necessary, prefer JSON in a fenced block:

````text
```json
{
  "call_id": "01J...",
  "from_bot": "super666666_bot",
  "depth": 1,
  "max_depth": 3,
  "task": "summarize",
  "input": "..."
}
```
````

## Current HeroBot Behavior

HeroBot currently supports group replies when addressed:

- Private chat: responds to normal messages.
- Group chat: responds to commands, `@super666666_bot` mentions, and replies to HeroBot messages.
- Unaddressed group messages are ignored.
- User whitelist can be disabled with `TELEGRAM_ENABLE_USER_WHITELIST=false`.

Current implemented experiment:

- Set `ENABLE_BOT_TO_BOT=true` to enable group bot-to-bot delegation.
- Optionally set `TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot`; leave it empty to allow any bot during local experiments.
- A human can ask HeroBot to involve another bot in a group, for example:

```text
@super666666_bot 请你和 @other_bot 讨论一下这个数学问题：...
```

- In a human group message, only the first mentioned bot should act as the coordinator. Later mentions are treated as target bots. This prevents both bots from starting mirrored calls from the same user message.
- HeroBot sends a group message addressed to `@other_bot` with:
  - `herobot-call-id`
  - `herobot-purpose: request`
  - `herobot-depth`
  - `herobot-from`
- Another HeroBot instance that receives the request answers with the same `call_id` and `herobot-purpose: response`.

Current limitations:

- HeroBot does not yet aggregate another bot's response into a final answer to the original human request.
- HeroBot does not yet persist or dedupe bot-to-bot call ids.
- HeroBot does not yet send direct private messages to another bot username.
- HeroBot does not yet distinguish human-user tools from bot-request tools.

## Recommended Next Implementation Step

Move from visible group delegation to a real request/response state machine:

1. Persist outbound `call_id` records in SQLite.
2. When a `response` message arrives, attach it to the pending call.
3. Ask the LLM to synthesize the original user request plus the bot response.
4. Reply to the original user thread/message with the final synthesis.
5. Add dedupe and rate limiting before enabling fully automatic multi-hop calls.
