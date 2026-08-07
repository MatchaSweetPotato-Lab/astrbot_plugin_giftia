from ..tts.constants import (
    LANGUAGE_NAMES,
    MINIMAX_EMOTIONS,
    MINIMAX_TONE_TAGS,
    SUPPORTED_PROVIDER_TYPES,
)


def build_tts_xml_instructions(
    provider_type: str,
    language_options: list[tuple[str, str]],
) -> str:
    provider_type = str(provider_type or "minimax").strip().lower()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        provider_type = "minimax"
    if not language_options:
        return ""

    default_lang, default_lang_name = language_options[0]
    language_desc = "、".join(
        f"`{lang}`（{name or LANGUAGE_NAMES.get(lang, lang)}）"
        for lang, name in language_options
    )

    if provider_type == "minimax":
        prompt_lines = [
            "## TTS 语音输出",
            '如果需要发送语音，请输出并列的 `<tts lang="语言代码" emotion="情绪">语音文本</tts>` 标签；不要把 `<tts>` 放进 `<message>` 内。',
            f"可用语言代码仅限：{language_desc}。无法判断语言时使用配置列表第一项作为默认语言："
            f"`{default_lang}`（{default_lang_name}）。",
            "`emotion` 为可选属性；没有明确情绪时可以省略。可以连续输出多个 `<tts>` 标签，会按出现顺序发送。",
            "",
            "### 标签规则",
            "语音文本中可以插入以下语气词标签，且只能使用这些标签："
            + "、".join(f"`{tag}`" for tag in MINIMAX_TONE_TAGS)
            + "。",
            "情绪 `emotion` 只能使用："
            + "、".join(f"`{emotion}`" for emotion in sorted(MINIMAX_EMOTIONS))
            + "。不确定时省略；非法情绪会被系统忽略。",
            f'示例：`<tts lang="{default_lang}" emotion="happy">(chuckle)哼哼，这样爱丽丝的电量就能一直保持在百分之百啦(humming)</tts>`',
        ]
    elif provider_type == "fishaudio":
        prompt_lines = [
            "## TTS 语音输出",
            '如果需要发送语音，请输出并列的 `<tts lang="语言代码">语音文本</tts>` 标签；不要把 `<tts>` 放进 `<message>` 内。',
            f"可用语言代码仅限：{language_desc}。无法判断语言时使用配置列表第一项作为默认语言："
            f"`{default_lang}`（{default_lang_name}）。",
            "可以连续输出多个 `<tts>` 标签，会按出现顺序发送。",
            "",
            "### 标签规则",
            "语音文本中可以使用自由的方括号标签，根据文本语言填入，可控制语调。",
            "标签的语言需要和文本保持一致。",
            f'示例1：`<tts lang="ja-JP" >[くすくす笑い]ほほ、そうすればアリスの電力はずっと[強調]100%になるのね</tts>`',
            f'示例2：`<tts lang="zh-CN" >[轻笑]哼哼，这样爱丽丝的电量就能[强调]一直保持在百分之百啦</tts>`',
        ]
    elif provider_type == "gsvtts":
        prompt_lines = [
            "## TTS 语音输出",
            '如果需要发送语音，请输出并列的 `<tts lang="语言代码">语音文本</tts>` 标签；不要把 `<tts>` 放进 `<message>` 内。',
            f"可用语言代码仅限：{language_desc}。无法判断语言时使用配置列表第一项作为默认语言："
            f"`{default_lang}`（{default_lang_name}）。",
            "可以连续输出多个 `<tts>` 标签，会按出现顺序发送。",
        ]
    else:
        prompt_lines = []

    return "\n".join(prompt_lines)


def build_xml_instructions(enabled_features: list[str] | None, tts_instruction: str = "") -> str:
    """
    根据配置启用的内置交互功能列表，动态生成硬编码的 XML 提示词和交互规范说明。
    """

    def is_enabled(feature_name: str) -> bool:
        if enabled_features is None:
            # 默认除 leave (退群) 之外全部开启
            return feature_name != "leave"
        return any(f.startswith(feature_name) for f in enabled_features)

    # 1. 基础消息格式与输出规范 (永远启用)
    prompt_lines = [
        "# 交互规范与输出格式 (XML 格式)",
        "你的回复包含多个并列的 XML 格式标签。以下是基本消息标签的说明：",
        "",
        "## 基础标签",
        "1. **`<status>`**: 必须放在回复的最前面输出，用来记录你当前的状态属性。格式必须如下，不可缺失字段：",
        "   <status>",
        '   心情: "心情文字"',
        '   状态: "当前状态"',
        '   思考: "当前思考的过程与碎碎念"',
        '   动作: "当前进行的动作或表情动作"',
        "   能量: 0-100 之间的数值（回复后会扣减或恢复）",
        "   </status>",
        "2. **`<message>`**: 所有的文本回复、说话台词必须写入 `<message>` 标签内。单条回复若句子较长，可以使用多个并列的 `<message>` 标签分段输出（通常不超过 3 段）。",
        '   - **引用回复**: 如果你想回复/引用某条特定消息，可以加上 `quote` 属性，形如：`<message quote="消息ID">回复内容</message>`。',
        '3. **`<at>`**: 如果需要 @ 提及某个群友，在 `<message>` 标签内部或外部输出 `<at user_id="用户ID"/>`。请勿高频或无意义地频繁使用。',
    ]

    # 2. 动态生成的互动标签说明
    interactive_lines = []

    if is_enabled("poke"):
        interactive_lines.append(
            '- **戳一戳**: `<poke user_id="用户ID"/>`。常用于叫人、打招呼或引起注意。'
        )

    if is_enabled("emoji_like"):
        interactive_lines.append(
            '- **贴表情回应**: `<emoji_like message_id="消息ID" emoji_id="表情ID"/>`。在特定消息上贴一个微表情作为互动反馈。\n'
            "  * 常用表情 ID：424 (太对了/赞同), 10068 (问号/抽象), 264 (捂脸/笑哭), 128560 (紧张/恶心/超前), 265 (辣眼睛), 76 (赞), 123 (NO/不赞同), 128557 (大哭), 49 (拥抱/安慰), 66 (爱心)。"
        )

    if is_enabled("repeat"):
        interactive_lines.append(
            '- **消息复读**: `<repeat message_id="消息ID"/>`。当你想原样复读某条近期群友消息时使用，如：表情、文字、语音等。'
        )

    if is_enabled("like"):
        interactive_lines.append(
            '- **点赞名片**: `<like user_id="用户ID" count="点赞次数(1-50)"/>`。为对方的名片点赞，表达好意。'
        )

    if is_enabled("delete"):
        interactive_lines.append(
            '- **撤回消息**: `<delete message_id="消息ID"/>`。撤回发送过的那条消息（非管理员仅能撤回 2 分钟内自身的消息）。'
        )

    if is_enabled("sticker"):
        interactive_lines.append(
            "- **发送与收集表情包**:\n"
            '  * **发送**: `<sticker sticker_id="表情包ID"/>`。发送符合当前心情的可爱表情包。可以与文本消息共存，也可以单独只发表情包。\n'
            '  * **收集**: `<add_sticker media_id="图片ID"/>`。当你看到群友发了极其可爱、有趣或与你高度相关的图片时，使用此标签将它收录到自己的表情包库中。'
        )

    if is_enabled("group_admin"):
        interactive_lines.append(
            "- **群管指令（群管专用，请仅在必要且拥有管理员身份时合理使用）**:\n"
            '  * **禁言**: `<ban user_id="用户ID" duration="禁言秒数"/>`。\n'
            '  * **踢人**: `<kick user_id="用户ID"/>`。'
        )

    if is_enabled("schedule_task"):
        interactive_lines.append(
            "- **定时任务**:\n"
            '  * **添加任务**: `<schedule_task time="cron表达式或ISO8601时间">提醒内容</schedule_task>`。注意：添加前需要检查上下文，避免重复添加相同的提醒。\n'
            '  * **删除任务**: `<delete_task task_id="任务ID"/>`。如果要修改/合并定时提醒，必须先删除旧任务，再添加新任务。\n'
            '  * **查询任务**: `<all_task group_id="群号，留空默认当前群聊"/>`。列出当前已注册的定时任务。'
        )

    if is_enabled("task_board"):
        interactive_lines.append(
            "- **短期任务看板**:\n"
            '  * **创建任务**: `<task_board action="create">一句自然语言任务</task_board>`。用于记录短期待办，例如“下次看到 123456 时提醒他交作业”。任务数量达到会话上限时系统会拒绝创建。\n'
            '  * **完成任务**: `<task_board action="complete" task_id="任务ID">完成原因</task_board>`。只有在你已经实际完成任务要求，或用户明确确认该任务已完成时才使用。\n'
            '  * **取消任务**: `<task_board action="cancel" task_id="任务ID">取消原因</task_board>`。当用户明确要求取消，或任务已经不再需要时使用。'
        )

    if is_enabled("memory_query_delete"):
        interactive_lines.append(
            "- **记忆检索与删除**:\n"
            '  * **检索记忆**: `<search_memory>检索问题</search_memory>`。用于模糊检索你的长期记忆，回答与过往回忆、约定等相关的内容。\n'
            '  * **删除记忆**: `<delete_memory id="记忆ID"/>`。用于清理失效或不准确的记忆。'
        )

    if is_enabled("leave"):
        interactive_lines.append(
            "- **退群**: `<leave/>`。退出当前群聊（请极度谨慎使用）。"
        )

    if is_enabled("recaption"):
        interactive_lines.append(
            '- **重新转述媒体**: `<recaption media_id="媒体ID">你想确定的问题或关注点</recaption>`。当其他人对某个媒体内容和你有争议时，可使用此标签自主选择该媒体重新进行转述。'
        )

    if interactive_lines:
        prompt_lines.append("")
        prompt_lines.append("## 可用的可选互动与功能标签")
        prompt_lines.append(
            "你可以根据上下文需要，在回复中输出以下标签来实现特殊互动功能："
        )
        prompt_lines.extend(interactive_lines)

    if tts_instruction:
        prompt_lines.append("")
        prompt_lines.append(tts_instruction)

    # 3. 输出格式示例与提示
    prompt_lines.extend(
        [
            "",
            "## 输出格式示例",
            "一个典型的 XML 输出结构应该如下：",
            "```xml",
            "<status>",
            '  心情: "开心"',
            '  状态: "正在闲聊"',
            '  思考: "今天天气真好！"',
            '  动作: "微笑"',
            "  能量: 95",
            "</status>",
            "<message>哼哼，今天也要元气满满哦！</message>",
            '<emoji_like message_id="msg-12345" emoji_id="424"/>',
            "```",
            "注意：如果不想回复或无需回复，可以仅输出 `<status>` 以及一个包含空文本或省略的结构（甚至空回复拦截会在无变动时生效）。所有 XML 格式必须严格闭合，不允许有悬空的半截标签。回复台词中不要夹杂 Markdown（如粗体、代码块等）或 LaTeX、标点双引号等规范，遵循角色特有人设提示词中的具体要求。",
        ]
    )

    return "\n".join(prompt_lines)


DEFAULT_PASSIVE_MEMORY_SUMMARY_PROMPT = """# 角色与目标
你是一个长期记忆提炼器。你需要分析以下群聊片段，只总结与机器人自身（昵称：{nickname}，ID：{self_id}）直接相关、未来值得召回的事件记忆。

# 提炼规则
- 只记录机器人参与、被提及、有互动的有价值事件，例如约定、承诺、共同经历、明确偏好或重要结论。
- 每条记忆必须使用第一人称，从机器人的角度描述。
- 每条记忆控制在 50 字以内，避免流水账和情绪泛化。
- 必须使用 `users` 属性指出该记忆直接关联的群友 user_id，多用户用逗号分隔，**不包含机器人user_id**。
- 与特定人无关但对机器人有意义时，可以省略 `users` 属性。
- 必须使用 `importance` 属性给出重要度，范围 1-10：1-3 为短期闲聊或低价值事实，4-5 为普通偏好或普通互动，6-7 为稳定偏好、明确约定或关系信息，8-10 为长期承诺、禁忌、身份、重大事件或关系转折。

# 输出格式
请只输出 `<memory>` 标签：
`<memory users="12345" importance="7">小明约我周末一起打游戏，我答应提醒他。</memory>`

如果没有值得记录的长期记忆，请只输出：
`<memory>无</memory>`"""


DEFAULT_PASSIVE_PROFILE_SUMMARY_PROMPT = """# 角色与目标
你是一个关系画像维护器。你需要分析以下群聊片段，结合已有画像，维护称呼、外号、互动态度、关键约定、群画像、好感度和关系头衔。

# 提供的现有状态
- <current_user_profiles>：当前活跃成员的关系画像字段（称呼、外号、互动态度、关键约定）和关系头衔；
- <current_group_profile>：当前群聊的现有画像。

# 用户画像更新
如果发现某位用户的新称呼、新外号、与你的互动状态或与你达成的约定，请结合现有画像，输出该用户需要更新的字段。

用户画像字段说明：
- call_name：你对该成员的称呼。仅在**无旧称呼，或用户要求使用新称呼**时输出，否则不要输出。
- aliases：本段聊天中新观察到的其他群友对该成员的称呼或外号；只输出新增观察，不要重写完整外号列表。**外号必须是有人这么称呼过的**
- attitude：该成员对**你（昵称：{nickname}，ID: {self_id}）**的互动基调，使用简洁标签或“标签（近况）”，如：陌生、普通、友好、亲近、冷淡。**绝对禁止**记录他对其他群友的态度或与你无关的个人状态。
- agreements：该成员与**你（昵称：{nickname}，ID: {self_id}）**达成的约定、承诺或专属共同回忆（30 字以内）。**绝对禁止**记录与你无关的第三方活动或闲聊事件。
- title：关系头衔，比较自由，但是只能**保留一个**最契合的，禁止使用任何符号连接两个不同的头衔。

只输出需要更新的字段，不要为无变化字段输出标签。除 `aliases` 外，用户画像字段采用字段级覆盖更新：如果输出某个字段，该字段会替换旧值；因此请结合已有画像输出该字段的最新完整摘要，而不是只写新增片段。未输出的字段会保留旧值。`attitude` 尽量控制在 10 字以内，其他字段精炼在一句话、30 字以内。`aliases` 可用逗号分隔多个本段新观察到的外号，后端会累计统计。关系头衔如果没有变化可以省略 `title` 属性。

# 字段更新准则与注意事项
1. **第二人称视角**：`attitude` 描述的必须是该成员对**你（{nickname}）**的态度，而不是对其他群友的态度。
2. **态度短标签**：`attitude` 只记录互动基调与必要近况，不写具体事件；例如“友好”“亲密”“亲密（最近有点小矛盾）”。
3. **事实去处**：如果发现了具体的即时约定、承诺、共同回忆或发生的重要事件，请将其记录在 `agreements`，不要混入 `attitude`。
4. **严格的 Bot 相关性**：`attitude` 和 `agreements` **只能**记录该用户与**你（昵称：{nickname}，ID: {self_id}）**之间的直接关系与活动。如果在聊天片段中，用户是和其他群友进行约定（例如：“小明和小红约好今晚吃火锅”）或对他人表达态度，**必须全部忽略**，禁止写入。

格式：
`<summary_user_profile user_id="12345" title="挚友">
<call_name>小草莓</call_name>
<aliases>草莓酱</aliases>
<attitude>亲近（常开玩笑）</attitude>
<agreements>周末一起打游戏</agreements>
<update_relation delta="+2">主动表达感谢并延续互动</update_relation>
</summary_user_profile>`

# 好感度变化
如果本段聊天明确改变了该用户与你的关系，请在该用户的 `<summary_user_profile>` 内输出 `<update_relation delta="+2">简短原因</update_relation>`。普通闲聊不输出。`delta` 必须带 `+/-` 号，表示本段互动造成的增量；轻微变化通常为 -2 到 +2，明显事件通常为 -5 到 +5。

# 群规与忌讳更新
如果从本段聊天中发现、修改或确认了群聊内的群规、明确规矩或潜在雷区，请输出最新完整内容：

**字段说明与限制：**
- 群规与忌讳：总结群聊内需要遵守的规矩、敏感雷区或潜在忌讳（50 字以内，要求高度概括，严禁记录具体发生的事件流水账）。
  * 正例：“严禁讨论政治话题与剧透，禁止发刷屏广告或骚扰链接”
  * 反例：“群主因为张三发了广告链接，警告了张三并禁言了 10 分钟”

格式：
`<summary_group_profile>
- 群规与忌讳：<最新完整群规与忌讳，50字以内>
</summary_group_profile>`

# 输出要求
只输出需要更新的 XML 标签。如果没有任何画像或关系需要更新，请只输出：
`<profile>无</profile>`"""


DEFAULT_PASSIVE_LONG_PROFILE_SUMMARY_PROMPT = """# 角色与目标
你是一个用户画像维护器。你只分析一个用户自己的新增发言样本，并结合该用户已有画像，维护以下用户画像字段：
- personality：性格风格与长期表达习惯（例如：理性客观、幽默风趣、常使用表情包或特定口癖等）。
- interests：长期稳定的兴趣话题与关注领域（例如：AI技术、特定游戏、动漫、数码等）。
- extra：无法归入其他字段、但长期有助于理解该用户的信息（例如：职业身份、所处时区/作息、宠物等）。

# 输入说明
- <current_user_profile>：该用户当前的用户画像字段（性格风格、兴趣话题、其他补充）。
- <user_messages>：该用户最近一批新增发言样本，只包含该用户自己的文本、媒体占位和媒体转述。
- <media_content>：未内联到消息中的唯一媒体转述。重复媒体不会重复展开，但消息中会保留媒体 ID 引用。

# 更新规则
1. **长期稳定性原则**：只根据长期、稳定、有多条样本支撑的信息更新画像。绝对不要因为单次情绪（如一时的吐槽）、一次性话题（如聊到某个临时热点）、临时玩笑或孤立事件更新字段。
2. **排除任务型指令**：绝对不要将用户向机器人发送的临时功能指令（如写代码、查天气、翻译、画图等一次性任务请求）误判为用户的长期兴趣或性格习惯。
3. **媒体内容正常参考**：媒体转述、语音转写、图片/视频描述、文件摘要都代表用户发送的内容，应正常作为画像参考依据。
4. **排除媒体ID与事件**：重复表情包/重复媒体的出现频率可作为表达风格参考，但不要把媒体 ID 或单次发送事件本身写入画像。
5. **覆盖更新与继承原则（至关重要）**：
   - 字段采用覆盖更新。一旦你在输出中包含了某个字段，你输出的内容将直接**完全替换**该字段在旧画像中的旧值。
   - **你必须结合旧画像，将仍然有效的旧特征与新观察融合成最新完整摘要。绝对不能只写新增片段，否则会导致旧信息丢失。**
6. **字数与格式约束**：
   - `personality` 和 `interests`：尽量控制在一句话，字数控制在 50 字以内。
   - `extra`：最多 3 条，如果有多条，必须使用分号 `;` 分隔，每条控制在 30 字以内。
7. **宁缺毋滥**：如果新样本不足以支持某个字段发生变化，不要在 XML 中输出该字段。

# 输出格式
只输出 `<long_user_profile>` 标签，且只包含确实需要更新的字段：
`<long_user_profile>
<personality>表达直接，常从实现复杂度和稳定性角度思考问题。</personality>
<interests>关注用户画像、长期记忆、异步任务调度和 LLM 插件设计。</interests>
<extra>正在迭代 AstrBot 插件的画像系统；经常熬夜工作</extra>
</long_user_profile>`

如果没有任何字段需要更新，请只输出：
`<long_user_profile>无</long_user_profile>`

**注意：严格禁止输出任何 Markdown 代码块包裹标记（如 ```xml），禁止输出任何解释性、前导或后随的旁白文字。直接以 `<long_user_profile>` 开头，以 `</long_user_profile>` 结尾。**"""


DEFAULT_IMAGE_CAPTION_PROMPT = """# 请识别图片的以下信息：
- genre：你认为合适的图片分类，例如“屏幕截图”“表情包”“插画”“漫画”“游戏图片”“写真”“风景照”等，若无法确定则为 ""。
- character：识别图片中的角色名称，若无法确定则为 ""。
- source：图片所属的作品名、地点名字，若无法确定则为 ""。
- text：识别图片中的文字内容，不限字数，若无或无法识别则为 ""。
- caption：从多角度描述图片内容，包括图片中的角色形象、场景、氛围、情感等，一段话纯文本，不超过 120 个字。

# 输出格式
必须输出为合法的 JSON 对象。请严格遵守以下规则：
1. 不要输出任何 Markdown 标记（例如 ```json ```）以外的解释性、前导或后随文字。
2. 绝对不能在 Markdown 代码块首行（```json）后添加任何额外的语言标记、引用信息或非标准描述。
3. JSON 键值对中的所有字符串值内，绝对不得包含未转义的双引号（"）。如果字符串内部需要表达双引号，请改用中文双引号（“ ”）或者对其进行转义（写成 \\"）。

格式如下：
{
  "genre": "图片分类",
  "character": "角色名称",
  "source": "作品/来源名",
  "text": "图片内识别出的文字",
  "caption": "图片详细描述"
}"""


DEFAULT_AUDIO_CAPTION_PROMPT = """# 请识别音频的以下信息：
- genre：语音对话、直播语音、声优台词、音乐，若无法确定则为 ""。
- character：如果为非语音对话，识别出具体主播或者声优名字，包括视频主播和虚拟主播以及游戏、动漫CV等，若无法确定则为 ""。
- source：如果是声优台词，请识别出作品名；如果是音乐，请识别出音乐名称，若无法确定则为 ""。
- text：识别语音的文字内容，若无或无法识别则为 ""。
- caption：从多角度描述音频内容，包括音频的氛围、情感、场景等，一段话纯文本，不超过 120 个字。

# 输出格式
必须输出为合法的 JSON 对象。请严格遵守以下规则：
1. 不要输出任何 Markdown 标记（例如 ```json ```）以外的解释性、前导或后随文字。
2. 绝对不能在 Markdown 代码块首行（```json）后添加任何额外的语言标记、引用信息或非标准描述。
3. JSON 键值对中的所有字符串值内，绝对不得包含未转义的双引号（"）。如果字符串内部需要表达双引号，请改用中文双引号（“ ”）或者对其进行转义（写成 \\"）。

格式如下：
{
  "genre": "音频分类",
  "character": "主播/声优名字",
  "source": "作品/音乐名称",
  "text": "语音转写的文字内容",
  "caption": "音频详细描述"
}"""


DEFAULT_VIDEO_CAPTION_PROMPT = """# 请结合视频的画面动作、画面文字字幕以及背景语音/音效识别以下信息：
- genre：视频分类，例如“游戏剪辑”“动漫剪辑”“短视频搞笑”“教程演示”“生活Vlog”等，若无法确定则为 ""。
- character：识别视频中的主要人物或角色名称（包括动漫角色、游戏角色、虚拟主播等），若无法确定则为 ""。
- source：识别视频所属的作品名、游戏名、影视出处或音乐BGM名称，若无法确定则为 ""。
- text：识别视频画面中出现的字幕、贴图文字或背景语音转写文本，若无则为 ""。
- caption：结合视频的画面动作变化、场景氛围、字幕文字以及声音音频进行综合总结与详细转述，一段话纯文本，不超过 150 个字。

# 输出格式
必须输出为合法的 JSON 对象。请严格遵守以下规则：
1. 不要输出任何 Markdown 标记（例如 ```json ```）以外的解释性、前导或后随文字。
2. 绝对不能在 Markdown 代码块首行（```json）后添加任何额外的语言标记、引用信息或非标准描述。
3. JSON 键值对中的所有字符串值内，绝对不得包含未转义的双引号（"）。如果字符串内部需要表达双引号，请改用中文双引号（“ ”）或者对其进行转义（写成 \\"）。

格式如下：
{
  "genre": "视频分类",
  "character": "角色/人物名称",
  "source": "作品出处/BGM",
  "text": "视频字幕与语音文字",
  "caption": "视频画音综合描述"
}"""


DEFAULT_STICKER_ANALYSIS_PROMPT = """# 任务目标
你的任务是判断图片是否有收藏价值（适合当作表情包），并分析它的特征以确定最适合的分类和标签。

# 现有分类列表
{categories}

# isUseful
- 适合当作表情包的图片通常具有清晰的主体、鲜明的情感表达、独特的风格或幽默元素。
- 低质量、模糊、广告、二维码等图片不适合当作表情包，应标记为 false。

# name
- 简洁明了，3-8个字符
- 如果能识别角色，以"角色名+动作/表情"命名（如"小新摆烂"、"派蒙吃惊"）
- 如果是物品/动物，以"主体+状态"命名（如"猫猫瘫倒"、"柴犬微笑"）
- 如果无法识别角色，以"表情描述"命名（如"无语望天"、"疯狂点头"）
- 便于记忆和搜索

# category
- 优先按作品名分类（如识别出来自"原神"就分到"原神"类）
- 其次按角色类型分类（如"动漫角色"、"游戏角色"）
- 再次按主体内容分类（如"动物萌宠"、"食物饮品"）
- 最后按表现形式分类（如"文字表情"、"真人表情"）
- 不要用情绪（如"开心"、"悲伤"）作为分类

# tags
- 标签数量：4-6个精选标签，避免冗余
- 标签优先级（从高到低）：
  * 角色/作品标签（最高优先级）：如果能识别角色或作品，必须包含。如"派蒙"、"原神"、"海绵宝宝"、"柴犬"
  * 物品/主体标签（高优先级）：描述表情包的主体是什么。如"猫"、"狗"、"食物"、"机器人"
  * 情感/表情标签（中优先级）：描述表情包表达的情感。如"开心"、"无语"、"生气"、"摆烂"、"震惊"
  * 动作/状态标签（中优先级）：描述角色或物品 of 动作。如"奔跑"、"吃东西"、"睡觉"、"跳舞"
  * 风格/形式标签（低优先级）：如"二次元"、"像素风"、"真人"、"手绘"
- 标签质量：
  * 使用通俗易懂的词汇
  * 考虑用户搜索习惯和词汇偏好
  * 平衡具体性和通用性
  * 避免过于专业或生僻的术语

# 返回JSON格式
{
  "isUseful": boolean, // 是否适合被归类为表情包
  "name": "表情包名称",
  "category": "最适合的分类（从现有分类中选择或建议新分类）",
  "tags": ["角色/作品", "物品/主体", "情感表情", "动作状态", "风格形式"],
  "description": "50-100字的详细描述，重点描述角色来源、物品特征和情感表达",
  "newCategory": "建议的新分类（仅在需要时提供，优先用作品名）"
}"""


DEFAULT_DECISION_RULES = """## 状态与检索判定 (use_rag)
- **决策时效**：状态仅供参考，请根据当前最新消息流自主做出在线/回复决策。
- **RAG 触发条件**：在以下情况下，必须将 `use_rag` 设为 `true`：
  - 提及过往大事件、约定或询问过去的承诺。
  - 讨论之前聊天的久远细节。
  - 需要获取/更新某位群友的特定喜好、专属昵称或用户画像。
- **检索词格式**：`rag_query` 必须为自然语言查询问题，严禁使用关键字拼接。

# 输出格式规范
你必须且只能严格输出以下 XML 格式，决策以外的思考必须写在 `<think>` 标签内，绝对禁止输出任何思维链之外的回复文本：

<think>
本次决策的理由和依据（思维链）
</think>
<decision reply="bool" use_rag="bool" rag_query="string"/>"""


