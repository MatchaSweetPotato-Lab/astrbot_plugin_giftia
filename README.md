<div align="center">

# Giftia

![:访问量](https://count.getloli.com/@astrbot_plugin_giftia?name=astrbot_plugin_giftia&theme=rule34&padding=5&offset=0&scale=1&pixelated=1&darkmode=auto)

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.27.0%2B-75B9D8.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Giftia](https://img.shields.io/badge/Giftia-v0.2.6-FFD700.svg)](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia)

</div>

**Giftia** 是一款面向 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的高性能、专注于聊天场景的人格与记忆沉淀插件。

> “赋予 AI 人格与记忆，以时间沉淀情感，用记忆塑造行为。”

通过本插件，你可以轻松让你的 Bot 拥有更像人类的情感记忆与聊天逻辑，实现智能的主动插嘴/接话、关系画像维护、好感度增减、长短期记忆 RAG 检索，以及极低 token 消耗的媒体转述系统。此外，它还配备了全功能的可视化 Web 仪表盘（Dashboard），让数据与缓存管理尽在掌控——包括决策审计、长期记忆、媒体转述、表情包管理、画像与 Token 统计。

> [!IMPORTANT]
> **核心使用建议**
> - **关闭原生 AI 对话**：启用本插件时，推荐在 AstrBot 管理面板中**完全关闭“AI 对话总开关”**。由本插件完全接管 AI 的对话与接话决策逻辑，避免回复冲突或重复回复。
> - **电脑能力与 Skills 限制说明**：由于关闭了原生 AI 对话管线，AstrBot 内置的**“使用电脑能力”（Computer Use：Shell、Python、文件系统、沙盒环境等）**以及依赖该环境的 **Skills 技能**将无法在 Giftia 对话中调用。Giftia 专注于人设陪伴、多轮记忆与拟人社交场景。
> - **硬件与 CPU 指令集要求 (AVX2)**：本插件依赖的向量数据库（LanceDB）在 x86 架构下需要 CPU 支持 **AVX2 指令集**（Intel 4代酷睿 Haswell 及以上，AMD 锐龙/挖掘机及以上；ARM64 / Apple Silicon 原生支持）。若在不支持 AVX2 的老旧 CPU（如 AMD Piledriver、Intel Sandy Bridge 等）上运行，插件在启动时会自动检测并直接抛出异常拒绝加载，以保护 AstrBot 本体稳定运行，避免底层触发非法指令（SIGILL 132）导致进程闪退（若确信环境误报，可在插件配置中开启 `ignore_avx2_check` 强制跳过）。
> - **网页搜索支持**：如需使用 Tavily 网页搜索，可先在 AstrBot 配置中填写好 API Key 并开启搜索，再关闭 AI 对话总开关。
> - **支持平台**：目前仅支持onebot（QQ）、QQ官方websocket，建议使用 onebot（QQ），官方会缺少部分交互功能，以及无法发送小图表情包（表情会以图片形式发送，在会话窗口占用大量空间）。这一条可以缓解：到 Web 面板「表情包管理」页签为该机器人开启**「以 GIF 格式发送」**，发送前会把表情包统一转成 GIF，官方 QQ 客户端便会按表情包渲染。开关每个机器人独立。

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
     - 配置小模型主动接话审查（决策 Prompt、接话概率、白名单、关键词规则）。
     - 配置大模型回复 使用的 AstrBot 人格 与提供商优先级列表。
     - 配置 TTS 语音合成供应商、语言映射及标志性语音。
     - 勾选允许该机器人调用的内置 XML 互动工具（如戳一戳、复读、点赞名片、表情包发送等）。

2. **在 AstrBot 插件设置中配置基础设施模型**
   - **媒体转述配置**：为图片、音频与视频转述配置相应的多模态模型供应商（如 Gemini 等）。
   - **记忆检索与重排配置**：配置 **嵌入模型** 与 **重排模型** 的供应商及模型以开启长期 RAG 记忆。
   - **被动状态维护**：开启“启用被动状态维护”，并在 **被动总结模型提供商** 中配置相应的模型提供商，以自动提炼聊天记忆、好感度与用户画像。

---

## 状态、用户画像与报告模板

在 AstrBot 插件配置的 **报告渲染配置 → 渲染模式** 中选择 **文本响应**（默认）或 **图片响应**。`/查看状态`、`/状态` 和 `/画像` 共用此配置；图片渲染失败时自动回退到文本。

- `/查看状态` 或 `/状态`：返回当前群聊或私聊的机器人状态。
- `/画像 @一位用户` 或 `/画像 123456789`：查询指定用户的画像，展示称呼、外号、性格、兴趣、互动态度、约定、补充、头像描述及好感度和关系称谓。@ 用户优先使用消息中的真实用户 ID；也支持直接输入平台用户 ID。

画像只查询**当前机器人、当前群聊或私聊**已有的记录，不跨会话查询，也不会触发新的画像生成；未找到时会提示暂无记录。每次只能查询一位用户。

在插件 WebUI 的 **报告模板** 页签中，选择 **状态报告** 或 **用户画像**，分别编辑 HTML / Jinja2 模板，停止输入约半秒后自动更新示例预览。展开变量说明可查看支持的字段，展开预览数据可修改示例 JSON；这些数据仅用于预览，指令始终读取当前会话的实际记录。点击 **保存模板** 后生效，**载入默认模板** 会先载入编辑器，保存后才替换现有模板。切换页签会保留未保存的草稿。

点击 **上传图片** 添加素材，再点击素材将图片标签插入编辑器光标处。支持 PNG、JPEG、WebP、GIF，每张不超过 2 MB、1600 万像素，共最多 100 张；GIF 使用首帧。图片统一转换为 WebP，并通过 `{{ asset('素材名称.webp') }}` 内嵌，也可用于 CSS 背景。模板支持内联 CSS，不执行 JavaScript，不加载外部图片、字体或样式；常规模板变量默认进行 HTML 转义。

**t2i 试渲染** 使用当前编辑器内容和示例数据调用 AstrBot 的实际渲染服务。正式出图同样调用 AstrBot `html_render`，沿用 AstrBot 配置的 t2i 服务；浏览器预览与 t2i 的字体和视口可能不同，以实际出图为准。[AstrBot HTML 渲染说明](https://docs.astrbot.app/en/dev/star/guides/html-to-pic.html)

自定义模板和图片分别保存在插件数据目录的 `reports/<报告类型>.html` 与 `reports/assets/` 下，重载插件后保留。默认状态模板位于 `core/reports/templates/status.html`，默认画像模板位于 `core/reports/templates/user_profile.html`；两种自定义模板分别存储为 `status.html` 和 `user_profile.html`。

后续扩展其他报告时，使用 `plugin.reports.register(report_type, ReportDefinition(...))` 注册默认模板、示例数据和字段说明，再由对应指令构建数据并调用 `await plugin.reports.render_image(report_type, data)`。新报告类型自动出现在编辑器中，共用模板存储、图片素材、预览和 t2i 渲染流程。返回值为临时图片路径，由调用方在发送或读取完成后清理；数据构造示例见 `core/reports/status.py` 和 `core/reports/user_profile.py`。

## 更多详情与高级使用

关于完整的常用命令列表、所有配置项的详细说明以及提示词模版详解，请参阅：
- [**Giftia 详细使用文档 (DETAIL.md)**](DETAIL.md)
- [**媒体缓存与第三方插件适配指南 (MEDIA_CACHE_ADAPTATION.md)**](MEDIA_CACHE_ADAPTATION.md)

## 交流与反馈

- **bot拷打群**：123180736

## 致谢
感谢 Codex 和 Google One PRO 提供代码补全与参考支持！
