# HeroBot 代码评审

## 总体评价

HeroBot 是一个有野心的项目——基于 ReAct agent 架构的 Telegram 个人助理，支持笔记、待办、日程、提醒、联系人、甚至 bot 间协作约时间。架构设计方向正确（MCP 分层、平台工具和业务工具分离、agent loop 和 Telegram adapter 解耦），说明你对系统设计有一定理解。

但在工程实践和用户视角上有比较多的改进空间。下面分几个维度展开。

---

## 一、从用户角度出发思考（这是最重要的一点）

很多问题的根源在于：写代码时没有站在用户的角度去体验。建议每写完一个功能，自己当用户用一遍，问自己"如果我不是开发者，这个体验合理吗？"

### 1.1 错误信息对用户不友好

`bot.py:313` 直接把 Python 异常暴露给用户：

```python
await update.effective_message.reply_text(f"Agent 执行失败：{exc}")
```

用户看到 `Agent 执行失败：'title'`（KeyError）或 `Agent 执行失败：Connection refused` 完全不知道发生了什么。更严重的是，这可能泄露内部实现细节（数据库路径、API 地址等）。

**建议**：给用户一个固定的友好提示，详细错误只写日志。

### 1.2 时间显示不直观

`tools.py` 的 `find_availability` 返回 UTC ISO 字符串。用户（或 LLM）看到的是 `2026-06-04T01:00:00+00:00`，而不是 `2026-06-04 09:00 (北京时间)`。虽然 LLM 理论上会帮忙转换，但这是不可靠的——LLM 有时会算错时区。

**建议**：工具返回结果中同时包含 UTC 和本地时间的可读格式。

### 1.3 提醒精度和格式

`scheduler.py` 的轮询间隔 30 秒，意味着提醒最多延迟 30 秒。用户设了 14:00 的提醒，可能 14:00:29 才收到。提醒内容也太简单——只有 `提醒：{content}`，没有时间上下文。

### 1.4 长消息处理

Telegram 限制消息 4096 字符。如果 LLM 生成了超长文本，`send_telegram_message` 会直接失败。应该自动截断或分段发送。

### 1.5 多步操作缺少进度反馈

agent 执行多步工具调用时（比如约时间流程涉及 5+ 步），用户只在最开始看到"正在输入"的提示，之后就是漫长等待。应该在关键节点给用户一些中间反馈。

### 1.6 搜索能力薄弱

笔记搜索用 `LIKE '%term%'` 全表扫描，停用词是硬编码的中文短语列表，不支持语义搜索。用户存了"证件位置"但搜"护照在哪"就可能搜不到。

---

## 二、架构和设计问题

### 2.1 Storage 层：每次操作都开新连接

这是最大的性能问题。`storage.py` 里每个方法都 `async with aiosqlite.connect(self.db_path) as db:`，每次都走 connect/close。一次用户请求可能触发 5-10 次 storage 调用，每次都开关连接。

**建议**：在 `init()` 时创建持久连接，所有方法复用。

### 2.2 巨大的 if/elif 分发链

`tools.py` 的 `_invoke` 方法有 ~18 个 if 分支，每加一个工具要同时改 `tools.py`、`mcp_server.py`（加 @mcp.tool 包装）、可能还有 `storage.py`——三个文件联动。

**建议**：用注册表模式或装饰器模式，加新工具只需要写一个方法并注册。

### 2.3 MCP server 的样板代码

`mcp_server.py` 里 ~20 个 @mcp.tool 函数全是样板——每个函数只是把参数打包成 dict 然后调用 invoke()。200+ 行代码，实际逻辑为零。

**建议**：用元编程自动生成 MCP tool 注册，或者让 BusinessTools 的方法直接用装饰器注册。

### 2.4 关注点混淆

- `parse_scheduling_confirmation` 是纯业务逻辑，但放在 `bot.py`（Telegram adapter 层）。
- 它在运行时代码中甚至没有被调用，只在测试里用了——看起来是写了但忘了集成。
- `owner_user_ids = allowed_user_ids` 把"允许使用的人"和"bot 主人"等同了，概念上是错的。

### 2.5 全局可变状态

`mcp_server.py` 用 module-level 全局变量 + `global` 关键字做懒初始化。不可测试、不可重置、没有清理机制。

### 2.6 配置管理松散

`bot.py` 的 `build_application` 把配置塞进 `bot_data` 字典，用字符串 key 访问，没有类型检查。拼错 key 名只有运行时才发现。

**建议**：定义 BotConfig dataclass，所有配置项都是带类型的字段。

---

## 三、生产可靠性问题

### 3.1 LLM 调用没有重试和超时

LLM API 调用（OpenAI 或兼容 API）经常出现 429、500、网络超时。当前任何失败都会直接变成用户可见的错误。

**建议**：加指数退避重试（tenacity 库），加超时设置。

### 3.2 MCP tool 调用没有超时

MCP server 子进程如果卡住，`await self._session.call_tool()` 会永远挂起，导致整个 agent 停止响应。如果 MCP server 进程崩溃，`_session` 还是非 None，后续调用会得到管道错误。

**建议**：加 `asyncio.wait_for` 设置超时，加健康检查或重连机制。

### 3.3 没有可观测性

整个项目没有：
- 结构化日志（当前只有 basicConfig）
- 指标采集（请求量、延迟、错误率、LLM token 消耗）
- agent 行为审计（用户说了什么、agent 调了哪些工具、结果是什么）
- 健康检查端点

在出问题时，你没有任何工具来诊断。

### 3.4 数据库没有迁移策略

当前用 `CREATE TABLE IF NOT EXISTS` 建表。加新字段、改类型、加索引都需要手动迁移。没有 schema version 追踪。

**建议**：引入 alembic 或至少维护一个 schema_version 表。

### 3.5 工具参数没有校验

所有工具参数都是 `dict[str, Any]`，没有任何校验。如果 LLM 漏传了必需参数，直接 KeyError 崩溃。

**建议**：用 pydantic 或 dataclass 做参数校验，给友好错误提示。

---

## 四、代码风格问题

### 4.1 `del context` anti-pattern

`bot.py` 里的 `del context` 只是为了消除 unused parameter 警告。惯用做法是用 `_context` 命名。

### 4.2 函数内部 import

`parse_scheduling_confirmation` 在函数体内 `import re`。应该放在文件顶部。

### 4.3 摘要传消息用 Python repr

`llm.py` 的 `summarize` 方法里 `f"新消息：{messages}"` 直接把 list[dict] 转成 Python repr 传给 LLM，格式不友好。

---

## 五、测试覆盖

测试写得还行，特别是有 MCP end-to-end 的测试和 agent runtime 的集成测试。但：

1. 所有测试放在一个类里，没有按模块拆分
2. 缺少负面测试（空输入、无效参数、异常情况）
3. 缺少并发和性能测试
4. 建议迁移到 pytest（语法更简洁、fixture 更灵活）
5. FakeLLM 只能验证"按预设步骤执行"，无法验证"正确理解用户意图"

---

## 六、安全方面

1. 错误信息可能泄露内部实现细节（数据库路径、API 错误等）
2. `context_from_payload` 默认 chat_id=0，如果上下文注入失败会静默写入错误位置
3. bot-to-bot 通信虽然有白名单机制，但没有频率限制（.env 里有 BOT_TO_BOT_RATE_LIMIT_PER_MINUTE 的建议但没有实现）
4. 消息历史没有清理策略，数据库会无限增长

---

## 七、积极方面（做得好的地方）

1. **架构分层清晰**：Telegram adapter / Agent runtime / Platform tools / MCP business tools 的分层是对的
2. **MCP 集成**：用 MCP 暴露业务工具、隐藏上下文注入的思路很好
3. **安全意识**：用户白名单、bot 白名单、finish_task 强制收束、max_steps 保险丝
4. **bot-to-bot 协作**：约时间的多步流程设计得比较完整
5. **文档和 demo**：README、demo.md、telegram-bot-to-bot-communication.md 写得较详细
6. **测试**：有端到端集成测试，不只是单元测试

---

## 八、优先改进建议（按重要性排序）

1. **用户体验**：错误信息友好化、时间本地化显示、长消息分段 —— 这些直接影响用户感受
2. **Storage 连接复用**：消除每次操作开关连接的开销
3. **LLM 调用重试 + 超时**：这是生产环境最常见的故障源
4. **工具参数校验**：用 pydantic 防止 KeyError 到达用户
5. **消除重复代码**：用注册表模式重构 tools.py + mcp_server.py 的分发逻辑
6. **日志和可观测性**：加结构化日志、agent 行为审计
7. **数据库迁移**：引入迁移框架或至少版本追踪

---

## 关于 AI 工具的使用建议

当前代码中有很多重复性的样板代码（mcp_server.py 的 20 个 @mcp.tool 函数、tools.py 的 if/elif 链），这些可以利用 AI 辅助生成或重构。一些建议：

1. **用 AI 做代码审查**：写完代码后让 AI 从用户角度 review，发现体验问题
2. **用 AI 生成样板代码**：重复的 MCP tool 注册、参数校验逻辑可以让 AI 批量生成
3. **用 AI 写测试**：特别是负面测试用例，AI 很擅长想到你没想到的边界情况
4. **用 AI 做文档**：API 文档、错误消息的国际化等
5. **不要过度依赖 AI**：架构决策、安全设计、用户体验这些核心问题还是要自己思考

总的来说，项目架构方向正确，功能也比较完整。主要的改进方向是：**多从用户角度思考体验、加强生产级的错误处理和可靠性、减少重复代码**。加油。
