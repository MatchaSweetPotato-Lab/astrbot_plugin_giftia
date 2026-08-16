# Giftia 媒体缓存机制与第三方插件适配指南

本文档介绍 Giftia 插件内置的媒体缓存与哈希 ID 索引机制，并为第三方插件开发者（特别是提供 LLM Tool 工具调用的插件）提供具体的适配指南与代码示例。

---

## 背景说明

由于 AstrBot 框架原生针对临时多媒体文件（图片、音频、语音等）的缓存周期较短，在多轮对话、异步任务或稍后发起的大模型工具调用（LLM Tools）中，原生暂存路径可能已失效或被过早清理。

为了保障拟人化聊天、记忆沉淀与多模态转述的稳定性，Giftia 插件内部实现了一套独立的**持久化媒体缓存与哈希索引机制**：

1. **自动持久化缓存**：所有经过消息管线的媒体文件（图片、音频、视频）都会被下载并持久化保存至 Giftia 专属的数据目录下。
2. **唯一哈希索引**：为每个媒体生成 16 位十六进制的稳定哈希 ID（例如 `a1b2c3d4e5f67890`）。
3. **上下文占位符替换**：Bot 在对话上下文中看到的媒体消息均被规范化为 `[图片:哈希ID]`、`[语音:哈希ID]` 或 `[视频:哈希ID]` 格式。

---

## 对其他插件的影响

当 AstrBot 启用了 Giftia，大模型在决策并调用其他插件注册的 LLM Tools（如生图、图生图、表情包制作、OCR、识图等）时：
- 大模型传入工具参数的图片/媒体标识，通常会直接携带上下文中的 16 位哈希 ID，或者形如 `[图片:a1b2c3d4e5f67890]` 的占位符文本。
- 如果第三方插件仅支持常规 `http(s)://` 链接或本地绝对应路径，会导致无法识别该参数并报错（如提示“文件不存在”或“无效的图片地址”）。

---

## 适配方案与代码示例

第三方插件只需在解析入参时，增加对 16 位哈希 ID 的识别并从 Giftia 缓存目录中读取文件即可完成无缝兼容。

### 1. 媒体缓存存储路径

Giftia 的媒体缓存统一存放在 AstrBot 数据目录下的 Giftia 插件子目录中：

```python
from pathlib import Path
from astrbot.core.star.star_tools import StarTools

# 标准获取方式
giftia_cache_dir: Path = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache"
```

该目录下直接以 16 位哈希值作为文件名存储对应媒体原始二进制数据（如 `data/plugins/astrbot_plugin_giftia/media_cache/a1b2c3d4e5f67890`）。

### 2. 通用媒体解析辅助函数

以下是一个推荐的通用解析函数，可同时兼容 Giftia 哈希 ID、占位符、网络 URL、本地文件路径及 Base64 字符串：

```python
import os
import re
import urllib.parse
from pathlib import Path
from astrbot.core.star.star_tools import StarTools

# 匹配 16 位十六进制哈希或占位符
HASH_PATTERN = re.compile(r"(?:\[(?:图片|语音|视频):)?([a-fA-F0-9]{16,64})\]?")

def resolve_media_to_path(media_input: str) -> Path | str | None:
    """
    将用户或 LLM 输入的媒体标识转换为可访问的本地文件路径或 URL。
    
    支持格式：
    1. Giftia 媒体哈希 ID (如 "a1b2c3d4e5f67890" 或 "[图片:a1b2c3d4e5f67890]")
    2. 网络 URL (以 http:// 或 https:// 开头)
    3. 本地文件绝对路径 / 相对路径
    4. file:// 协议路径
    """
    if not media_input or not isinstance(media_input, str):
        return None
    
    media_input = media_input.strip()
    
    # 1. 检查是否为本地有效文件路径
    local_path = media_input.removeprefix("file://")
    if os.path.exists(local_path):
        return Path(local_path)
    
    # 2. 检查是否为 Giftia 媒体哈希 ID
    match = HASH_PATTERN.search(media_input)
    if match:
        hash_val = match.group(1).lower()
        giftia_cache_file = StarTools.get_data_dir("astrbot_plugin_giftia") / "media_cache" / hash_val
        if giftia_cache_file.exists():
            return giftia_cache_file
        
        # 兼容相对路径回退
        fallback_path = Path("data/plugins/astrbot_plugin_giftia/media_cache") / hash_val
        if fallback_path.exists():
            return fallback_path
    
    # 3. 如果是网络 URL，直接返回由下游处理或自行下载
    if media_input.startswith(("http://", "https://")):
        return media_input
    
    return None
```

### 3. LLM 工具参数描述优化

在注册 FunctionTool 时，建议在参数描述（description）中明确提示大模型可以传入哈希 ID，以获得最佳的模型调用准确度：

```python
{
    "name": "your_image_tool",
    "description": "图像处理工具",
    "parameters": {
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": "目标图片地址，支持网络 URL、本地路径或上下文中的图片哈希 ID（例如 a1b2c3d4e5f67890 或 [图片:a1b2c3d4e5f67890]）。"
            }
        },
        "required": ["image"]
    }
}
```

---

## 已适配插件参考

以下插件已原生适配 Giftia 媒体缓存机制，开发者可参考其实现：

1. **大香蕉画图插件**：[astrbot_plugin_big_banana](https://github.com/sukafon/astrbot_plugin_big_banana)
2. **表情包管理插件**：[astrbot_plugin_meme_manager](https://github.com/Yao-lin101/astrbot_plugin_meme_manager)
