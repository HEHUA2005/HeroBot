# HeroBot Demo 测试用例

这份文档用于快速验证 HeroBot 的轻量个人助理能力，以及两个私人助理 bot 在共同群聊里协商时间的 MVP 流程。

## 0. 测试前准备

### 单 bot 私聊

确认 `.env` 已配置：

```bash
TELEGRAM_BOT_TOKEN=你的bot token
TELEGRAM_ALLOWED_USER_IDS=你的Telegram user id
OPENAI_BASE_URL=localhost:4000
OPENAI_API_KEY=你的LLM API key
HEROBOT_DB_PATH=data/herobot.sqlite3
HEROBOT_OWNER_NAME=你的名字
HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_WORKING_HOURS=09:00-18:00
ENABLE_BOT_TO_BOT=false
```

启动：

```bash
herobot
```

### 两个 bot 群聊协作

准备两个实例 env，例如：

```text
instances/super666666.env
instances/hehuaone.env
```

每个实例需要：

```bash
TELEGRAM_BOT_TOKEN=不同bot token
HEROBOT_DB_PATH=data/不同数据库.sqlite3
HEROBOT_OWNER_NAME=不同主人名
ENABLE_BOT_TO_BOT=true
TELEGRAM_ENABLE_USER_WHITELIST=false
```

启动：

```bash
herobot-multi --env-dir instances
```

把两个 bot 都拉进同一个 Telegram 群聊。测试完建议把白名单重新打开。

## 1. 基础身份和群聊检查

### 查看自己的 Telegram user id

私聊 bot：

```text
/whoami
```

期望：

```text
你的 Telegram user id 会被返回。
```

### 查看当前 chat id

在私聊或群聊：

```text
/chatid
```

期望：

```text
返回 chat id 和 chat type。群聊中 chat type 应该是 group 或 supergroup。
```

## 2. 普通个人助理能力

### 待办

发送：

```text
我今天要买牛奶
```

再发送：

```text
我还有什么待办？
```

期望：

```text
bot 能创建待办，并在查询时列出来。
```

### 笔记

发送：

```text
帮我记一下：护照放在书桌右边抽屉里
```

再发送：

```text
我护照在哪里？
```

期望：

```text
bot 能从持久化笔记里查到护照位置。即使发送 /reset 清空会话上下文，也仍然应该查得到。
```

### 提醒

发送：

```text
提醒我 1 分钟后喝水
```

期望：

```text
bot 先确认提醒已创建，约 1 分钟后主动推送提醒。
```

## 3. 联系人通讯录

### 添加联系人

私聊主 bot：

```text
添加联系人 李雷 @HEHUAone_bot
```

期望：

```text
bot 确认已保存联系人。
```

### 查询联系人

发送：

```text
李雷的助理是谁？
```

或：

```text
/contacts
```

期望：

```text
能看到 李雷 -> @HEHUAone_bot。
```

### 删除联系人

发送：

```text
删除联系人李雷
```

期望：

```text
bot 确认联系人已删除。再次查询时不应出现李雷。
```

## 4. 个人日程

### 创建忙碌事件

发送：

```text
我明天 14:00-16:00 有会
```

期望：

```text
bot 创建日程事件。自然语言时间由 LLM 理解，代码只接收结构化时间写入数据库。
```

### 查看日程

发送：

```text
/calendar
```

期望：

```text
能看到刚刚创建的事件。
```

### 查看空闲时间

发送：

```text
/availability
```

期望：

```text
bot 返回默认工作时间内的空闲时间段。
```

如果想测试明确时间范围，可以发送：

```text
/availability 2026-06-04 09:00-18:00
```

期望：

```text
bot 在指定范围内计算空闲时间。
```

## 5. 两个助理协商约时间

这个场景需要两个 bot 都在同一个群聊里。

### 准备两边联系人

对主 bot 私聊：

```text
添加联系人 李雷 @HEHUAone_bot
```

对另一个 bot 私聊：

```text
添加联系人 我 @super666666_bot
```

### 给双方制造不同日程

对主 bot 私聊：

```text
我明天 14:00-15:00 有会
```

对另一个 bot 私聊：

```text
我明天 15:30-16:30 有事
```

### 在共同群聊发起协商

在群聊里发送：

```text
@super666666_bot 帮我和李雷约明天下午 30 分钟
```

期望流程：

```text
1. 主 bot 根据通讯录找到 @HEHUAone_bot。
2. 主 bot 在群里自然地询问对方助理的可用时间。
3. 对方 bot 只返回可用时间段，不暴露自己的具体日程标题、地点或原因。
4. 主 bot 结合双方空闲时间，给主人列出 1-3 个候选。
5. 候选不会自动写入日程，必须等主人确认。
```

### 确认候选

主人在群聊或私聊主 bot：

```text
确认第 1 个时间
```

期望：

```text
主 bot 把该候选写入自己的日程。
```

再发送：

```text
/calendar
```

期望：

```text
能看到新创建的约会事件。
```

### 取消协商

发起一次新的约时间后，不确认，发送：

```text
取消这次约时间
```

期望：

```text
pending session 被取消，不写入日程。
```

## 6. 权限和安全测试

### 非主人确认

让群里另一个 Telegram 用户发送：

```text
确认第 1 个时间
```

期望：

```text
bot 应拒绝或忽略。不能让别人替主人确认日程。
```

### 未添加联系人时约时间

删除联系人后，在群聊发送：

```text
@super666666_bot 帮我和李雷约明天下午 30 分钟
```

期望：

```text
bot 应提示找不到联系人，要求先添加联系人。
```

### 信息不足时约时间

发送：

```text
@super666666_bot 帮我和李雷约一下
```

期望：

```text
bot 应追问缺失信息，例如时间范围或时长，而不是瞎猜。
```

## 7. bot-to-bot 普通讨论

在共同群聊发送：

```text
@super666666_bot 请你和 @HEHUAone_bot 讨论一下：先有鸡还是先有蛋？
```

期望：

```text
1. 只有第一个被 @ 的 bot 作为主控方发起。
2. 另一个 bot 被动回应。
3. 群消息中不应出现 call_id、depth 等协议字段。
4. 输出应尽量是纯文本，不要依赖 Markdown 表格。
```

## 8. 常见问题

### 出现 409 Conflict

含义：

```text
同一个 Telegram bot token 被两个进程同时 polling。
```

处理：

```bash
ps aux | rg herobot
```

停掉重复进程，确保每个 token 只启动一个实例。

### bot 在群里不回复

检查：

```text
1. bot 是否被拉进群。
2. BotFather privacy mode 是否影响普通消息接收。
3. 群里是否 @ 了正确的 bot username。
4. 白名单是否关闭，或当前用户是否在 TELEGRAM_ALLOWED_USER_IDS 中。
5. ENABLE_BOT_TO_BOT 是否为 true。
```

### 约时间没有自动写入日程

这是预期行为。

```text
协商结果必须由主人发送“确认第 N 个时间”后才会写入日程。
```
