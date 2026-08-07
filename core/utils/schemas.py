from dataclasses import dataclass, field
import re

from astrbot.core.message.components import BaseMessageComponent

FORWARD_MEDIA_PATTERN = re.compile(r"\[(?:图片|语音|视频):([^\]\s]+)\]")
FORWARD_NESTED_PATTERN = re.compile(r"\[合并转发:([^\]\s]+)\]")


def extract_media_ids(content: str) -> list[str]:
    if not content:
        return []
    return FORWARD_MEDIA_PATTERN.findall(content)


def extract_nested_forward_ids(content: str) -> list[str]:
    if not content:
        return []
    return FORWARD_NESTED_PATTERN.findall(content)


def normalize_memory_importance(value, default: int = 5) -> int:
    """Normalize memory importance into the 1-10 range."""
    try:
        if value is None or value == "":
            normalized = int(default)
        else:
            normalized = int(float(value))
    except (TypeError, ValueError):
        normalized = int(default)
    return max(1, min(10, normalized))


@dataclass(repr=False, slots=True)
class MessageData:
    db_id: int = 0
    nickname: str = ""
    user_id: str = ""
    group_or_user_id: str = ""
    time: str = ""
    message_id: str = ""
    content: str = ""
    is_recalled: int = 0  # 0: 未撤回, 1: 已撤回
    media_id_list: list[str] = field(default_factory=list)  # 这里只存储媒体ID
    forward_messages: list[dict] = field(default_factory=list)
    role: str = "message"  # "message" or "operation_log"


@dataclass(repr=False, slots=True)
class MediaCaption:
    hash_val: str = ""
    file_name: str = ""
    url: str = ""
    media_type: str = ""  # image, video, audio
    genre: str = ""
    character: str = ""
    source: str = ""
    text: str = ""
    caption: str = ""
    is_captioned: bool = True
    duration: float = 0.0
    file_size: int = 0


@dataclass(repr=False, slots=True)
class Status:
    mood: str = ""
    state: str = ""
    memory: str = ""
    action: str = ""
    energy: str = ""
    timestamp: float = 0.0
    last_updated: float = 0.0


@dataclass(repr=False, slots=True)
class Decision:
    reply_decision: int = 2  # 0: 决策拒绝, 1: 决策通过, 2: 未决策
    use_rag: int = 2  # 0: 不使用RAG, 1: 使用RAG, 2: 未决策
    rag_query: str = ""


@dataclass(repr=False, slots=True)
class MemoryItem:
    memory_id: str
    text: str
    vector: bytes
    metadata: str
    updated_at: str
    created_at: str
    importance: int = 5
    hit_count: int = 0
    last_hit_at: str = ""


@dataclass(repr=False, slots=True)
class SessionRecallMemory:
    memory_id: str
    text: str
    metadata: str = "{}"
    score: float = 0.0
    distance: float = 1.0
    hit_count: int = 1
    first_recalled_at: float = 0.0
    last_recalled_at: float = 0.0
    updated_at: str = ""
    created_at: str = ""


@dataclass(repr=False, slots=True)
class Sticker:
    sticker_id: str
    name: str
    category: str
    tags: list[str]
    description: str
    filename: str = ""


@dataclass(repr=False, slots=True)
class ShortTask:
    task_id: str
    bot_name: str
    group_or_user_id: str
    creator_user_id: str
    creator_nickname: str
    content: str
    status: str = "active"
    closed_by_user_id: str = ""
    close_reason: str = ""
    expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(repr=False, slots=True)
class TTSRequest:
    text: str
    lang: str = ""
    emotion: str = ""
    pre_recorded_path: str = ""


@dataclass(repr=False, slots=True)
class BotSticker:
    timestamp: float  # 缓存时间戳
    sticker_list: list[str]  # 完整的sticker_id列表
    sticker_set: set[str]  # 完整的sticker_id集合


@dataclass(repr=False, slots=True)
class XmlLlmResult:
    status: Status = field(default_factory=Status)
    # 这个主要是给aiocqhttp用的，其他平台可能没这么多功能
    msg_chains: list[list[BaseMessageComponent]] = field(default_factory=list)
    msg_logs: list[str] = field(default_factory=list)  # AI自身消息的消息链日志
    # 给aiocqhttp发送失败后降级以及其他平台用的文本消息
    msg_texts: list[str] = field(default_factory=list)
    # 同样是aiocqhttp用的，只不过消息链没这个组件就独立了出来
    delete_message_ids: list[str] = field(default_factory=list)
    # 贴表情，同样是给aiocqhttp用的
    emoji_ids: list[tuple[str, str]] = field(default_factory=list)
    # 消息复读，同样是给aiocqhttp用的
    repeat_message_ids: list[str] = field(default_factory=list)
    # 点赞，同样是给aiocqhttp用的
    likes: list[tuple[str, str]] = field(default_factory=list)
    # 戳一戳，同上。群号，用户ID
    poke: list[tuple[str, str]] = field(default_factory=list)
    # 禁言，同上。群号，用户ID，时长(秒)
    ban: list[tuple[str, str, str]] = field(default_factory=list)
    # 踢人，同上。群号，用户ID
    kick: list[tuple[str, str]] = field(default_factory=list)
    # 退群，同上。群号
    leave: list[str] = field(default_factory=list)
    # 长期记忆。群号/用户ID，内容
    search_memories: list[tuple[str, str]] = field(default_factory=list)
    delete_memories: list[str] = field(default_factory=list)
    # 工具调用
    tools_to_call: list[tuple[str, dict]] = field(
        default_factory=list
    )  # (工具名, 工具参数)
    # 原生 function calling / tool loop 已调用的工具名
    native_tools_called: list[str] = field(default_factory=list)
    # 定时任务，群号/用户ID，时间，内容
    schedule_tasks: list[tuple[str, str, str]] = field(default_factory=list)
    # 删除定时任务，任务ID
    delete_schedule_tasks: list[str] = field(default_factory=list)
    # 获取全部定时任务，群号
    all_tasks: list[str] = field(default_factory=list)
    # 添加表情包，媒体ID
    add_stickers: list[str] = field(default_factory=list)
    # 发送表情包，表情ID
    send_stickers: list[str] = field(default_factory=list)
    # 历史记录搜索
    search_histories: list[dict] = field(default_factory=list)
    # 消息上下文查询
    get_message_contexts: list[dict] = field(default_factory=list)
    # 短期任务看板操作
    task_board_actions: list[dict] = field(default_factory=list)
    # TTS 语音输出
    tts_segments: list[TTSRequest] = field(default_factory=list)
    # 重新转述媒体请求
    recaption_requests: list[dict] = field(default_factory=list)
    # 保留 message / tts 在 LLM XML 中的出现顺序
    output_order: list[tuple[str, int]] = field(default_factory=list)
