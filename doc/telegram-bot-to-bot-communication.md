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
- Keep Telegram platform tools scoped to the current chat.
- Require the Agent to explicitly finish each event with `finish_task`.
- Add a max Agent step count as an execution fuse.
- Add per-chat and per-bot rate limits.
- Do not let another bot directly trigger privileged personal-data tools unless explicitly allowed.

Suggested `.env` shape:

```env
ENABLE_BOT_TO_BOT=true
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot
HEROBOT_MAX_AGENT_STEPS=8
BOT_TO_BOT_RATE_LIMIT_PER_MINUTE=20
```

## Current HeroBot Behavior

HeroBot currently supports group replies when addressed:

- Private chat: responds to normal messages.
- Group chat: responds to commands, `@super666666_bot` mentions, and replies to HeroBot messages.
- Unaddressed group messages are ignored.
- User whitelist can be disabled with `TELEGRAM_ENABLE_USER_WHITELIST=false`.

Current implemented experiment:

- Set `ENABLE_BOT_TO_BOT=true` to let allowed bot messages enter the Agent runtime.
- Optionally set `TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot`; leave it empty to allow any bot during local experiments.
- A human can ask HeroBot to involve another bot in a group. The Agent decides whether to call `send_telegram_message`, for example:

```text
@super666666_bot 帮我问问 @other_bot 能做什么
```

- In a human group message, only the first mentioned HeroBot should process the event. Later mentions are available to the Agent as possible targets.
- HeroBot no longer emits visible `herobot-call-id`/`herobot-depth` protocol fields for ordinary bot-to-bot messages.
- Business capabilities are exposed through the local MCP server; Telegram messaging is an internal platform tool.

Current limitations:

- HeroBot does not yet send direct private messages to another bot username.
- Bot-to-bot response aggregation is Agent-driven and depends on the active LLM choosing the right tools.
- There is not yet a persisted cross-message task ledger for multi-step bot collaborations.

## Recommended Next Implementation Step

Add a persisted Agent task ledger:

1. Persist outbound platform messages and their originating user request.
2. When a bot reply arrives, attach it to the pending task.
3. Give the Agent the pending task context explicitly.
4. Add dedupe and rate limiting before enabling fully automatic multi-hop calls.
