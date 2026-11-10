<div align="center">

# Giftia

![:访问量](https://count.getloli.com/@astrbot_plugin_giftia?name=astrbot_plugin_giftia&theme=rule34&padding=5&offset=0&scale=1&pixelated=1&darkmode=auto)

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.27.0%2B-75B9D8.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Giftia](https://img.shields.io/badge/Giftia-v0.5.5-FFD700.svg)](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia)

</div>

**Giftia** 是一款面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的高性能、专注于聊天场景的人格与记忆沉淀插件。

> “赋予 AI 人格与记忆，以时间沉淀情感，用记忆塑造行为。”

通过本插件，你可以轻松让你的 Bot 拥有更像人类的情感记忆与聊天逻辑，实现智能的主动插嘴/接话、原子事实记录与原话依据保存、轻量关系维护、好感度增减、长期记忆 RAG 检索，以及极低 token 消耗的媒体转述系统。此外，它还配备了全功能的可视化 Web 仪表盘（Dashboard），让数据与缓存管理尽在掌控——包括决策审计、长期记忆、媒体转述、表情包管理、画像与 Token 统计。

> [!IMPORTANT]
> **核心使用建议**
> - **关闭原生 AI 对话**：启用本插件时，推荐在 AstrBot 管理面板中**完全关闭“AI 对话总开关”**。由本插件完全接管 AI 的对话与接话决策逻辑，避免回复冲突或重复回复。
> - **电脑能力与 Skills 限制说明**：由于关闭了原生 AI 对话管线，AstrBot 内置的**“使用电脑能力”（Computer Use：Shell、Python、文件系统、沙盒环境等）**以及依赖该环境的 **Skills 技能**将无法在 Giftia 对话中调用。Giftia 专注于人设陪伴、多轮记忆与拟人社交场景。
> - **硬件与 CPU 指令集要求 (AVX2)**：本插件依赖的向量数据库（LanceDB）在 x86 架构下需要 CPU 支持 **AVX2 指令集**（Intel 4代酷睿 Haswell 及以上，AMD 锐龙/挖掘机及以上；ARM64 / Apple Silicon 原生支持）。若在不支持 AVX2 的老旧 CPU（如 AMD Piledriver、Intel Sandy Bridge 等）上运行，插件在启动时会自动检测并直接抛出异常拒绝加载，以保护 AstrBot 本体稳定运行，避免底层触发非法指令（SIGILL 132）导致进程闪退（若确信环境误报，可在插件配置中开启 `ignore_avx2_check` 强制跳过）。
> - **网页搜索支持**：如需使用 Tavily 网页搜索，可先在 AstrBot 配置中填写好 API Key 并开启搜索，再关闭 AI 对话总开关。
> - **支持平台**：目前仅支持onebot（QQ）、QQ官方websocket，建议使用 onebot（QQ），官方会缺少部分交互功能，以及**无法跨群共享画像、记忆、互动记录**。

---

## 核心理念：类“御坂网络”的群体记忆架构

Giftia 采用类似**“御坂网络（Misaka Network）”**的双层架构——**每个群聊的机器人上下文互相独立，但会在后台延迟共享总结的长期记忆与群友互动记录，机器人也可以自主通过相同群友的共有会话临时调取未同步的记录**：

| 架构层级 | 类“御坂网络”概念 | Giftia 运行机制 |
| :--- | :--- | :--- |
| **实时个体层**<br>*(各会话上下文独立)* | 每个群聊/私聊中的 Bot 都是独立的“个体节点”，只专注于眼前的对话现场 | **动态上下文窗口**、**接话活跃窗口**、**消息合并批次**、**实时状态/思考/能量**、**群规**以及**群内专属外号**均按会话严格隔离，实时聊天绝不串群。 |
| **网络同步层**<br>*(记忆与互动延迟共享)* | 当一段经历告一段落后，个体会将提炼出的记忆与人际羁绊异步上传至共享网络 | 不实时同步原始聊天记录；当已见消息达到阈值或活跃期结束（静默巡检封存片段）后，由后台**被动状态维护**异步提炼并跨会话同步：<br>1. **总结的长期记忆**：提炼带原话依据与关联群友的原子事实/事件。在其他群聊或私聊遇到同一群友时通过 RAG 召回（内部上下文注明外群/私聊来源）。<br>2. **群友互动记录与画像**：好感度、用户画像、历史互动评价在同一 Bot 下按用户跨群共享（未单独设置本群称呼时自动沿用最近设置的称呼；对外 `/画像` 指令仅展示本群记录，兼顾熟人感与边界感）。 |
| **可控的同步强度** | 按需调节网络连接范围 | 支持配置 `recall_scope`（记忆召回范围）：<br>• **用户隔离 (`user`，默认)**：按认识的群友跨群同步相关记忆，无特定关联人的群内事件仅留在本群。<br>• **完全检索 (`all`)**：同一 Bot 全网记忆互通。<br>• **会话隔离 (`session`)**：断开跨群记忆同步，仅在单群内独立检索。<br>*(注：QQ 官方机器人因平台 `openid` 机制限制，无法跨会话识别同一用户)* |

在此基础上，Giftia 配合**前置小模型接话决策**、**会话动态批处理**（见[会话动态批处理](docs/zh/session-batching.md) / [English](docs/en/session-batching.md)）与**按需媒体转述**，在保持高拟人感的同时大幅降低 Token 消耗。

---

## 快速开始

### 1. 安装插件
- 在 AstrBot 插件市场中搜索 `Giftia` 并点击安装。
- 或者在 AstrBot 插件页面中，点击 **+**，选择“从链接安装”，填写本项目地址进行安装。

### 2. 必要配置指南
完成以下两步配置即可开始使用：

1. **在 WebUI 控制台中配置机器人 (推荐)**
   - 在 AstrBot 插件页面打开插件 WebUI，切换至 **机器人管理** 页签，点击 **+ 新增机器人** 或编辑已有机器人：
     - 设置机器人唯一名称 (`name`) 与显示昵称 (`nickname`)。
     - 绑定消息平台适配器 ID（从已识别适配器列表中选择对应的 AstrBot 机器人账号）。
     - 配置**小模型主动接话审查**（决策 Prompt、接话概率、兴趣关键词规则与会话黑白名单）。
     - 配置**正式回复**使用的 AstrBot 人格与模型提供商列表。
     - 按需配置 TTS 语音合成供应商，以及允许调用的内置 XML 互动工具（戳一戳、复读、点赞名片、表情包发送、屏蔽用户等）。

2. **在 AstrBot 插件设置中配置基础设施模型**
   - **媒体转述配置**：在「媒体转述提供商」中选择日常图片/音频转述与表情包分析模型，可额外在「媒体查看工具提供商」中为 `inspect_media` 按需查看与视频理解配置更强的多模态模型。非活跃会话默认仅缓存媒体并保留占位符，Bot 需要时再通过工具按需查看。
   - **记忆检索与重排配置**：配置 **嵌入模型 (Embedding)** 与 **重排模型 (Rerank)** 以开启长期 RAG 记忆，并按需选择「记忆召回范围」（默认按用户跨群召回）。
   - **被动状态维护**：开启「启用被动状态维护」，并配置 **被动总结模型提供商**。Bot 会在后台自动将已见对话片段归纳为长期原子记忆，并在发生有效互动时维护群友外号、好感度与互动评价记录。

关于完整命令列表、所有配置项细则及提示词模版说明，请参阅：
- [**Giftia 详细使用文档 (DETAIL.md)**](DETAIL.md)
- [**媒体缓存与第三方插件适配指南 (MEDIA_CACHE_ADAPTATION.md)**](MEDIA_CACHE_ADAPTATION.md)
- [**版本变更与升级说明 (changelog.md)**](changelog.md)

---

## 媒体缓存机制与第三方插件适配

由于 AstrBot 原生的媒体缓存机制往往会过早清理临时媒体文件，导致多轮对话或异步任务中媒体丢失，Giftia 内置了一套独立的**持久化媒体缓存与哈希索引机制**：
- 对话上下文中出现的媒体消息（图片、语音、视频）会被持久化缓存，并转换为唯一的 16 位哈希 ID 索引（如 `[图片:a1b2c3d4e5f67890]`）。
- **对其他插件的影响**：当大模型调用其他插件注册的 LLM Tools（如画图、生图、表情包制作、识图等）时，传入的图片/媒体参数会直接是该哈希 ID 或占位符。若其他插件未进行适配，可能会因无法识别路径而报错。

### 第三方插件适配指南
第三方插件仅需添加简单判定，根据哈希 ID 从 Giftia 缓存目录中读取文件即可完成兼容。详细适配说明与代码示例请参阅：
- [**Giftia 媒体缓存与第三方插件适配指南 (MEDIA_CACHE_ADAPTATION.md)**](MEDIA_CACHE_ADAPTATION.md)

### 已适配插件列表
以下插件已原生适配 Giftia 的哈希 ID 媒体缓存机制，推荐搭配使用：
1. **大香蕉画图插件**：[astrbot_plugin_big_banana](https://github.com/sukafon/astrbot_plugin_big_banana)
2. **表情包管理插件**：[astrbot_plugin_meme_manager](https://github.com/Yao-lin101/astrbot_plugin_meme_manager)

## 交流与反馈

- **bot拷打群**：123180736

## 致谢
感谢 Codex 和 Google One PRO 提供代码补全与参考支持！
