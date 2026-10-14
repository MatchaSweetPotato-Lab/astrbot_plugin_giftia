<div align="center">

# Giftia

![:访问量](https://count.getloli.com/@astrbot_plugin_giftia?name=astrbot_plugin_giftia&theme=rule34&padding=5&offset=0&scale=1&pixelated=1&darkmode=auto)

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.27.0%2B-75B9D8.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Giftia](https://img.shields.io/badge/Giftia-v0.3.9-FFD700.svg)](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia)

</div>

**Giftia** 是一款面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的高性能、专注于聊天场景的人格与记忆沉淀插件。

> “赋予 AI 人格与记忆，以时间沉淀情感，用记忆塑造行为。”

通过本插件，你可以轻松让你的 Bot 拥有更像人类的情感记忆与聊天逻辑，实现智能的主动插嘴/接话、原子记忆归纳与定期整理、轻量关系维护、好感度增减、长期记忆 RAG 检索，以及极低 token 消耗的媒体转述系统。此外，它还配备了全功能的可视化 Web 仪表盘（Dashboard），让数据与缓存管理尽在掌控——包括决策审计、长期记忆、媒体转述、表情包管理、画像与 Token 统计。

> [!IMPORTANT]
> **核心使用建议**
> - **关闭原生 AI 对话**：启用本插件时，推荐在 AstrBot 管理面板中**完全关闭“AI 对话总开关”**。由本插件完全接管 AI 的对话与接话决策逻辑，避免回复冲突或重复回复。
> - **电脑能力与 Skills 限制说明**：由于关闭了原生 AI 对话管线，AstrBot 内置的**“使用电脑能力”（Computer Use：Shell、Python、文件系统、沙盒环境等）**以及依赖该环境的 **Skills 技能**将无法在 Giftia 对话中调用。Giftia 专注于人设陪伴、多轮记忆与拟人社交场景。
> - **硬件与 CPU 指令集要求 (AVX2)**：本插件依赖的向量数据库（LanceDB）在 x86 架构下需要 CPU 支持 **AVX2 指令集**（Intel 4代酷睿 Haswell 及以上，AMD 锐龙/挖掘机及以上；ARM64 / Apple Silicon 原生支持）。若在不支持 AVX2 的老旧 CPU（如 AMD Piledriver、Intel Sandy Bridge 等）上运行，插件在启动时会自动检测并直接抛出异常拒绝加载，以保护 AstrBot 本体稳定运行，避免底层触发非法指令（SIGILL 132）导致进程闪退（若确信环境误报，可在插件配置中开启 `ignore_avx2_check` 强制跳过）。
> - **网页搜索支持**：如需使用 Tavily 网页搜索，可先在 AstrBot 配置中填写好 API Key 并开启搜索，再关闭 AI 对话总开关。
> - **支持平台**：目前仅支持onebot（QQ）、QQ官方websocket，建议使用 onebot（QQ），官方会缺少部分交互功能，以及无法发送小图表情包（表情会以图片形式发送，在会话窗口占用大量空间）。这一条可以缓解：到 Web 面板「表情包管理」页签为该机器人开启**「以 GIF 格式发送」**，发送前会把表情包统一转成 GIF，官方 QQ 客户端便会按表情包渲染。开关每个机器人独立。

---

## 媒体缓存机制与第三方插件适配

Giftia 保持原有消息处理优先级，完成名单检查、指令识别和发送记录挂钩后立即返回；媒体解析、接话判断和正式回复在后台执行，后续插件无需等待这些步骤完成。Giftia 接管的普通消息会禁用 AstrBot 默认 AI 回复，避免后台处理期间重复回复，但不会停止消息向其他插件传播。指令和未被 Giftia 接管的消息保留原有默认 AI 行为。

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

---

## 快速开始

### 1. 安装插件
- 在 AstrBot 插件市场中搜索 `Giftia` 并点击安装。
- 或者在 AstrBot 插件页面中，点击 **+**，选择“从链接安装”，填写本项目地址进行安装。

### 2. 必要配置指南
为了快速上手并使插件正常工作，请完成以下配置：

1. **在 WebUI 控制台中配置机器人 (推荐)**
   - 在 AstrBot 插件页面打开插件 WebUI。
   - 切换至 **机器人管理** 页签，点击 **+ 新增机器人** 或编辑已有机器人：
     - 设置机器人唯一名称 (`name`) 与显示昵称 (`nickname`)。
     - 绑定消息平台适配器 ID (插件已识别适配器列表，选择自己配置的 AstrBot 机器人即可)。
     - 配置小模型主动接话审查（决策 Prompt、接话概率、关键词规则）。
     - 配置正式回复 使用的 AstrBot 人格 与提供商优先级列表。
     - 配置 TTS 语音合成供应商、语言映射及标志性语音。
     - 勾选允许该机器人调用的内置 XML 互动工具（如戳一戳、复读、点赞名片、表情包发送等）。

2. **在 AstrBot 插件设置中配置基础设施模型**
   - **媒体转述配置**：在「媒体转述提供商」中统一选择图片、音频、视频转述及表情包分析使用的模型，支持按优先级切换；按模型能力分别开启图片、音频和视频转述，在「网络配置」中设置统一的「媒体转述重试上限」。非活跃会话默认延迟转述，媒体仅缓存并保留占位符，Bot 需要时通过 `inspect_media` 查看；被 @、处于活跃窗口或默认直接回复的私聊可自动转述图片和音频，视频始终按需查看。
   - **记忆检索与重排配置**：配置 **嵌入模型** 与 **重排模型** 的供应商及模型以开启长期 RAG 记忆。
   - **被动状态维护**：开启“启用被动状态维护”，并在 **被动总结模型提供商** 中配置相应的模型提供商，以自动归纳原子记忆、定期整理旧记忆，并维护外号、互动态度和好感度。

> **媒体配置升级提示**：原图片、音频、视频的模型和重试字段已合并，非活跃时延迟转述固定为默认行为。升级后请重新选择一次「媒体转述提供商」；不自动迁移旧配置。三类启用开关保留，关闭某类后不再发起相应模型请求，已有转述缓存仍可通过工具读取。


关于完整的常用命令列表、所有配置项的详细说明以及提示词模版详解，请参阅：
- [**Giftia 详细使用文档 (DETAIL.md)**](DETAIL.md)
- [**媒体缓存与第三方插件适配指南 (MEDIA_CACHE_ADAPTATION.md)**](MEDIA_CACHE_ADAPTATION.md)

## 交流与反馈

- **bot拷打群**：123180736

## 致谢
感谢 Codex 和 Google One PRO 提供代码补全与参考支持！
