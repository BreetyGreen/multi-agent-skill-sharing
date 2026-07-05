# v2 设计方案：跨产品聊天记录同步（Chat History Sync）

> 状态：**已实现（MVP + HTML 时间线 + 会话接力 handoff）**。实现见 `scripts/sync_chats.py`、`scripts/handoff_chat.py` 与 `scripts/chatsync/`，用法见 `scripts/README_chatsync.md`。
> 定位：v1 解决「一个 skill 如何被多个 agent 复用」；v2 解决「多个 agent 的**聊天记录**如何跨产品汇总与同步」。
> 本文档基于在真实机器（macOS）上对 5 个 agent 的存储实测得出，非纸面推测。
> 探测日期：2026-07-05。**这些都是各产品未公开的内部存储，随版本变化，方案需持续校验。**

---

## 0. 一句话结论

- **只读汇总（导出/归档/检索）**：✅ 完全可行、风险低，**这是 v2 应该做的核心**。
- **真·双向注入（把 A 的旧记录带原 ID 硬塞进 B 的库冒充 B 的历史）**：⚠️ 技术上能写文件，但因 schema 有损、私有 ID 体系、运行时锁库、格式漂移四大硬伤，**不实用且高风险，v2 不做，仅在文档中说明为何不做**。
- **会话接力（handoff）**：✅ 已实现，**这是"换个产品接着聊"的正解**。把 A 的完整会话打包成一段可粘贴文本（前情+正文+接力指令），用户在 B 里**新开一条 B 自己现发合法新 ID 的会话**、粘贴、发送即可续聊。**不伪造 ID、不写任何 agent 的库**——它只读 A、产出文本。超长会话由 `auto` 模式自动降级为"摘要+关键轮次"以适配上下文长度。实现见 `scripts/handoff_chat.py` 与 `chatsync/handoff.py`。

---

## 1. 背景：为什么这比 v1 难一个量级

v1（skill 分发）本质是**复制一个静态 Markdown 文件**到不同目录——源和目标是同一种东西。

v2（聊天记录同步）本质是**跨多种异构数据库做 ETL**：每个产品用**完全不同的数据模型**存对话，没有任何公共 schema。要"同步"，必须做「解析 → 归一到中间格式 → 回写重建」，而回写这一步对多数产品是有损甚至危险的。

---

## 2. 实测：5 个 agent 的存储形态（本机真实数据）

| Agent | 存储位置 | 形态 | 单条结构 | 实测规模 | 读取难度 | 写回风险 |
|-------|----------|------|----------|----------|----------|----------|
| **Claude Code** | `~/.claude/projects/<路径编码>/<uuid>.jsonl` | 一会话一 JSONL 文件 | 每行 `{type, message, sessionId, parentUuid...}`，content 为分块数组（text/tool_use/tool_result） | 27 会话 | 低（纯文本） | 中（可追加，但父子链要对） |
| **WorkBuddy** | `~/.workbuddy/projects/<路径编码>/<uuid>.jsonl` | 一会话一 JSONL 文件 | 每行 `{id, parentId, logicalParentId, type, role, content, providerData}`，type ∈ message/function_call/... | 37 会话 | 低（纯文本） | 中 |
| **Codex CLI** | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（+ `archived_sessions/`） | 事件流日志 JSONL | 每行 `{timestamp, type, payload}`；payload 有 10+ 种：message/reasoning/function_call/function_call_output/token_count/task_started... | 65 会话 | 中（事件多、需重组） | 高（事件序列强耦合） |
| **Cursor** | `~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb` | SQLite KV | key `aiService.prompts`（list，实测 290 条）、`composer.composerData`（dict），消息拆成 bubble 散在 blob | 32 工作区 | 高（blob 不透明） | **极高（运行时锁库、易损坏）** |
| **Antigravity** | `~/Library/Application Support/Antigravity/User/workspaceStorage/<hash>/state.vscdb` | SQLite KV | key `chat.ChatSessionStore.index`（`{version, entries}`）；正文疑在 globalStorage/blob，**待进一步逆向** | 7 工作区 | 高 | **极高（同 Cursor）** |

### 2.1 关键发现（决定架构）

1. **Claude Code 与 WorkBuddy 近似同构**：都是「一会话一 `.jsonl`、每行一事件、带父子链」。→ **可共用一个 reader adapter**，只需处理 content 字段差异（Claude 分块数组 vs WorkBuddy `content`+`type`），工作量省近一半。
2. **Codex 是纯事件流**：信息最全（含 reasoning、token 统计），但要把散落的 message/function_call 事件**按时间重组**成"轮次"。
3. **Cursor / Antigravity 同属 SQLite-KV 路线**：都是 VSCode 内核，聊天散在 `state.vscdb` 的不同 key，都是不透明 blob，**运行时持锁**。→ 读必须用**只读模式** `sqlite3.connect("file:...?mode=ro", uri=True)`，**绝不写**。

---

## 3. 架构：Canonical 中间格式 + Adapter（N 而非 N×N）

不做产品两两互转（5 个产品要 20 个转换器），而是所有产品向一个**统一中间模型**归一：N 个 reader + N 个 writer 即可。

```
Claude ─┐                                      ┌─ (writer) Claude
WorkBuddy┤                                     ├─ (writer) WorkBuddy
Codex ──┼─(reader)→  Canonical 统一模型  →(writer)┼─ (writer) Codex
Cursor ─┤            会话/消息标准结构           ├─ Cursor：只读，不写回
Antigrav┘                                      └─ Antigravity：只读，不写回
```

### 3.1 Canonical schema（草案）

```json
{
  "schema_version": "1.0",
  "session_id": "canonical-uuid",
  "source_agent": "claude|workbuddy|codex|cursor|antigravity",
  "source_ref": "原始文件路径或 db key，供回溯",
  "project": "/abs/path/to/project",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "title": "首条用户消息摘要（自动生成）",
  "messages": [
    {
      "role": "user|assistant|system|tool",
      "timestamp": "ISO-8601",
      "text": "纯文本正文（各源统一抽取）",
      "blocks": [ {"kind":"text|tool_use|tool_result|reasoning", "raw": {}} ],
      "meta": { "model": "...", "tokens": 123 }
    }
  ],
  "lossy_notes": ["记录本次转换丢失了什么，如 reasoning 轨迹、内部索引"]
}
```

设计要点：
- `text` 是**最小公分母**（人可读、可全文检索），保证所有源都能填。
- `blocks` 保留原始结构化信息（有则填），供高保真场景使用。
- `lossy_notes` **显式记录有损转换**，不假装无损——这是诚实工程。
- `source_ref` 保证可回溯到原始数据。

---

## 4. 分模块可行性与风险

### 4.1 Reader（读取归一）——全部可行

| 源 | 实现要点 | 风险 |
|----|---------|------|
| Claude Code | 逐行 parse JSONL；按 parentUuid 排序；抽 content 数组里的 text/tool 块 | 低 |
| WorkBuddy | 同上；注意 `type=function_call` 与 message 混排，按 timestamp 归轮 | 低 |
| Codex | 按 timestamp 重放事件流；user_message/agent_message → 消息，function_call(_output) → tool 块，reasoning 单列 | 中：事件语义需吃透 |
| Cursor | **只读**打开 vscdb；解析 `composer.composerData` + bubble 关联；blob 反序列化 | 中：blob 结构随版本变 |
| Antigravity | **只读**打开 vscdb；`chat.ChatSessionStore.index` 定位会话，正文落点**需继续逆向**（本机该工作区 entries 为空，未取到样本） | 中：正文位置未完全确认 |

### 4.2 Writer（回写）——分级处理

| 目标 | 结论 | 理由 |
|------|------|------|
| Claude Code | 谨慎可写（追加新 `.jsonl`） | 纯文本、格式已知；但伪造 sessionId/parentUuid 可能不被 UI 认，建议标记为"导入的只读归档会话" |
| WorkBuddy | 同 Claude | 同上 |
| Codex | 谨慎可写 | 事件序列强耦合，重建易出错，优先级最低 |
| **Cursor** | **不写** | SQLite 运行时锁库，blob 不透明含版本迁移标记，外部写入可致**整库损坏、应用打不开** |
| **Antigravity** | **不写** | 同 Cursor |

> 写回统一规则：**默认 `--dry-run`；真写前自动备份原文件/库；SQLite 只读；小批量、可回滚。**（沿用 v1 `distribute.py` 的 dry-run 传统。）

---

## 5. 为什么"真·双向注入"基本是死胡同（重要）

1. **转换必然有损**：Codex 的 reasoning 轨迹、token 统计塞不进 Claude 的分块模型；Cursor 的 composer 结构还原不成 JSONL 树。同步回去的会话是"残缺副本"。
2. **私有 ID / 索引体系**：每个产品用自己的 UUID、会话树、内部索引。伪造的会话即便格式正确，产品也可能**不认、不显示，或下次启动被清理**。
3. **运行时锁 + blob（Cursor/Antigravity）**：写入是**破坏性操作**，风险与收益完全不成比例。
4. **格式随版本漂移**：全是未公开内部存储，产品一升级就可能改结构。v1 里路径漂移顶多"skill 找不到"；v2 里格式漂移可能"损坏用户数据"。

结论：**投入产出比极差，v2 明确不做**，只在文档留证。

---

## 6. 推荐的 v2 产品形态（MVP → 增强）

**MVP（只读汇总）：**
1. `scripts/sync_chats.py`（沿用 v1 纯标准库、跨平台、`--dry-run` 风格）：
   - `--agents claude,workbuddy,codex,cursor,antigravity` 选择源；
   - reader 归一到 Canonical；
   - 导出到 `chat-archive/`：每会话一份 **Markdown**（人读）+ 一份 **JSON**（机读），按 `项目/agent/日期` 归档；
   - `--dry-run` 只列将导出什么，不落盘。
2. 归档目录可提交进 Git → **随仓库跨机器/跨队友走**（呼应 v1「install once, travels with Git」理念）。

**增强（可选）：**
3. 一个**本地 HTML 时间线**页面：把 5 个工具的对话按项目**合并展示 + 全文搜索**，纯静态、离线打开。
4. `--since <date>` 增量、去重（跨源同一对话可能重复）。

**明确不做：** 向 Cursor/Antigravity 写回；真·双向注入。

---

## 7. 落地里程碑

| 阶段 | 内容 | 风险 |
|------|------|------|
| M1 | 本设计文档 + Canonical schema 定稿（本文件） | 无 |
| M2 | Claude + WorkBuddy reader（同构，一并做）→ 导出 MD/JSON | 低 |
| M3 | Codex reader（事件流重组） | 中 |
| M4 | Cursor + Antigravity reader（SQLite 只读；Antigravity 正文落点逆向） | 中 |
| M5 | 本地 HTML 合并时间线 + 全文搜索 | 低 |
| M6（可选/谨慎） | Claude/WorkBuddy 只读归档会话写回（dry-run + 备份） | 中 |

---

## 8. 待解决 / 需继续验证

- [ ] Antigravity 聊天**正文**的确切落点（本机取到 index 但 entries 为空，需一个有真实对话的工作区取样）。
- [ ] Cursor `composer.composerData` 的 bubble → 消息完整关联规则（blob 内部结构）。
- [ ] Codex `reasoning` / `token_count` 事件是否纳入 Canonical（默认纳入 blocks，text 不含）。
- [ ] 跨源去重策略（同一段工作可能在多个工具里都聊过）。

---

_本方案遵循 v1 的工程价值观：纯标准库、跨平台、dry-run 优先、随 Git 分发、诚实标注有损与风险。_
