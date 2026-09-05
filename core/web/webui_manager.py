from .web_api import GiftiaWebApi


class WebUIManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self.web_api = GiftiaWebApi(plugin)

    def register_routes(self):
        ctx = self.plugin.context

        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media",
            view_handler=self.web_api.get_media,
            methods=["GET"],
            desc="Get media captions list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/detail/<hash_val>",
            view_handler=self.web_api.get_media_detail,
            methods=["GET"],
            desc="Get single media caption detail by hash",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/update",
            view_handler=self.web_api.update_media,
            methods=["POST"],
            desc="Update media caption text",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/delete",
            view_handler=self.web_api.delete_media,
            methods=["POST"],
            desc="Delete media caption",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/file/<hash_val>",
            view_handler=self.web_api.get_media_file,
            methods=["GET"],
            desc="Get cached media file by hash",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/file/b64/<hash_val>",
            view_handler=self.web_api.get_media_file_b64,
            methods=["GET"],
            desc="Get cached media file as base64 by hash",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/file/thumbnail/b64/<hash_val>",
            view_handler=self.web_api.get_media_file_thumbnail_b64,
            methods=["GET"],
            desc="Get cached media thumbnail as base64 by hash",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/genres",
            view_handler=self.web_api.get_media_genres,
            methods=["GET"],
            desc="Get all distinct media genres",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/cache/clean",
            view_handler=self.web_api.clean_media_cache,
            methods=["POST"],
            desc="Clean media files cache by criteria",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/cache/auto_clean/config",
            view_handler=self.web_api.get_auto_clean_config,
            methods=["GET"],
            desc="Get media cache auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/cache/auto_clean/config",
            view_handler=self.web_api.set_auto_clean_config,
            methods=["POST"],
            desc="Set media cache auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/media/cache/auto_clean/trigger",
            view_handler=self.web_api.trigger_auto_clean,
            methods=["POST"],
            desc="Manually trigger media cache auto cleanup",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories",
            view_handler=self.web_api.get_memories,
            methods=["GET"],
            desc="Get memories list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/filter_options",
            view_handler=self.web_api.get_memory_filter_options,
            methods=["GET"],
            desc="Get memory filter options",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/add",
            view_handler=self.web_api.add_memory,
            methods=["POST"],
            desc="Add new memory",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/update",
            view_handler=self.web_api.update_memory,
            methods=["POST"],
            desc="Update memory text",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/delete",
            view_handler=self.web_api.delete_memory,
            methods=["POST"],
            desc="Delete memory",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/clean/candidates",
            view_handler=self.web_api.get_memory_clean_candidates,
            methods=["POST"],
            desc="Preview memory cleanup candidates",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/clean",
            view_handler=self.web_api.clean_selected_memories,
            methods=["POST"],
            desc="Clean selected memories",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/auto_clean/config",
            view_handler=self.web_api.get_auto_clean_memory_config,
            methods=["GET"],
            desc="Get memory auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/auto_clean/config",
            view_handler=self.web_api.set_auto_clean_memory_config,
            methods=["POST"],
            desc="Set memory auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/memories/auto_clean/trigger",
            view_handler=self.web_api.trigger_auto_clean_memories,
            methods=["POST"],
            desc="Manually trigger memory auto cleanup",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/status",
            view_handler=self.web_api.get_bot_status,
            methods=["GET"],
            desc="Get bot status list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/status/fill_energy",
            view_handler=self.web_api.fill_energy,
            methods=["POST"],
            desc="Fill bot energy",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/status/update",
            view_handler=self.web_api.update_bot_status,
            methods=["POST"],
            desc="Update bot mood/state",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/task_board",
            view_handler=self.web_api.get_task_board,
            methods=["GET"],
            desc="Get short task board",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/task_board/update",
            view_handler=self.web_api.update_task_board,
            methods=["POST"],
            desc="Update short task",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/task_board/delete",
            view_handler=self.web_api.delete_task_board,
            methods=["POST"],
            desc="Delete short task",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/task_board/clear",
            view_handler=self.web_api.clear_task_board,
            methods=["POST"],
            desc="Clear short tasks by status",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/scheduled_tasks",
            view_handler=self.web_api.get_scheduled_tasks,
            methods=["GET"],
            desc="Get scheduled tasks list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/scheduled_tasks/update",
            view_handler=self.web_api.update_scheduled_task,
            methods=["POST"],
            desc="Update scheduled task content and time expression",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/scheduled_tasks/delete",
            view_handler=self.web_api.delete_scheduled_task,
            methods=["POST"],
            desc="Delete single scheduled task",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/scheduled_tasks/clear",
            view_handler=self.web_api.clear_scheduled_tasks,
            methods=["POST"],
            desc="Clear all scheduled tasks for a session",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history",
            view_handler=self.web_api.get_chat_history,
            methods=["GET"],
            desc="Get chat history list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/filter_options",
            view_handler=self.web_api.get_chat_history_filter_options,
            methods=["GET"],
            desc="Get chat history filter options",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/delete",
            view_handler=self.web_api.delete_chat_history,
            methods=["POST"],
            desc="Delete chat history for a session",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/message/delete",
            view_handler=self.web_api.delete_single_message,
            methods=["POST"],
            desc="Delete a single chat history message by database id",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/auto_clean/config",
            view_handler=self.web_api.get_auto_clean_chat_history_config,
            methods=["GET"],
            desc="Get chat history auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/auto_clean/config",
            view_handler=self.web_api.set_auto_clean_chat_history_config,
            methods=["POST"],
            desc="Set chat history auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/chat_history/auto_clean/trigger",
            view_handler=self.web_api.trigger_auto_clean_chat_history,
            methods=["POST"],
            desc="Trigger chat history auto cleanup",
        )

        ctx.register_web_api(
            route="/astrbot_plugin_giftia/forwards",
            view_handler=self.web_api.get_forwards,
            methods=["GET"],
            desc="Get merged forward message records",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/forwards/detail",
            view_handler=self.web_api.get_forward_detail,
            methods=["GET"],
            desc="Get merged forward message detail",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/forwards/filter_options",
            view_handler=self.web_api.get_forward_filter_options,
            methods=["GET"],
            desc="Get merged forward filter options",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/forwards/clean",
            view_handler=self.web_api.clean_old_forwards,
            methods=["POST"],
            desc="Clean old merged forward message records",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user",
            view_handler=self.web_api.get_user_profiles,
            methods=["GET"],
            desc="Get user profiles list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/filter_options",
            view_handler=self.web_api.get_user_profile_filter_options,
            methods=["GET"],
            desc="Get user profile filter options",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/update",
            view_handler=self.web_api.update_user_profile,
            methods=["POST"],
            desc="Update user profile",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/delete",
            view_handler=self.web_api.delete_user_profile,
            methods=["POST"],
            desc="Delete user profile",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases",
            view_handler=self.web_api.get_user_aliases,
            methods=["GET"],
            desc="Get user profile aliases",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/add",
            view_handler=self.web_api.add_user_alias,
            methods=["POST"],
            desc="Add user profile alias",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/count",
            view_handler=self.web_api.update_user_alias_count,
            methods=["POST"],
            desc="Update user profile alias count",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/delete",
            view_handler=self.web_api.delete_user_alias,
            methods=["POST"],
            desc="Delete user profile alias",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/auto_clean/config",
            view_handler=self.web_api.get_auto_clean_aliases_config,
            methods=["GET"],
            desc="Get user aliases auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/auto_clean/config",
            view_handler=self.web_api.set_auto_clean_aliases_config,
            methods=["POST"],
            desc="Set user aliases auto cleanup config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/user/aliases/auto_clean/trigger",
            view_handler=self.web_api.trigger_auto_clean_aliases,
            methods=["POST"],
            desc="Trigger user aliases auto cleanup",
        )

        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/group",
            view_handler=self.web_api.get_group_profiles,
            methods=["GET"],
            desc="Get group profiles list",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/group/filter_options",
            view_handler=self.web_api.get_group_profile_filter_options,
            methods=["GET"],
            desc="Get group profile filter options",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/group/update",
            view_handler=self.web_api.update_group_profile,
            methods=["POST"],
            desc="Update group profile",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/profiles/group/delete",
            view_handler=self.web_api.delete_group_profile,
            methods=["POST"],
            desc="Delete group profile",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/token/stats",
            view_handler=self.web_api.get_token_stats,
            methods=["GET"],
            desc="Get token usage stats",
        )

        ctx.register_web_api(
            route="/astrbot_plugin_giftia/token/clear",
            view_handler=self.web_api.clear_token_logs,
            methods=["POST"],
            desc="Clear token usage logs",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/token/auto_clean/config",
            view_handler=self.web_api.get_auto_clean_token_config,
            methods=["GET"],
            desc="Get token auto clean config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/token/auto_clean/config",
            view_handler=self.web_api.set_auto_clean_token_config,
            methods=["POST"],
            desc="Set token auto clean config",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/token/auto_clean/trigger",
            view_handler=self.web_api.trigger_auto_clean_token,
            methods=["POST"],
            desc="Manually trigger token log auto cleanup",
        )

        # Bot management APIs
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots",
            view_handler=self.web_api.get_bots,
            methods=["GET"],
            desc="Get bot configurations and metadata",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/save",
            view_handler=self.web_api.save_bot,
            methods=["POST"],
            desc="Create or update bot configuration",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/delete",
            view_handler=self.web_api.delete_bot,
            methods=["POST"],
            desc="Delete bot configuration",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/toggle",
            view_handler=self.web_api.toggle_bot,
            methods=["POST"],
            desc="Toggle bot enabled status",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/voice/upload",
            view_handler=self.web_api.upload_signature_voice,
            methods=["POST"],
            desc="Upload signature voice file",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/voice/list",
            view_handler=self.web_api.list_signature_voices,
            methods=["GET"],
            desc="List uploaded signature voice files",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/voice/file/b64",
            view_handler=self.web_api.get_voice_file_b64,
            methods=["POST"],
            desc="Get base64 data url of signature voice file",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/bots/voice/delete",
            view_handler=self.web_api.delete_signature_voice,
            methods=["POST"],
            desc="Delete signature voice file from disk",
        )

        # Sticker management APIs
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers",
            view_handler=self.web_api.get_stickers,
            methods=["GET"],
            desc="Get stickers list with filters",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/filter_options",
            view_handler=self.web_api.get_sticker_filter_options,
            methods=["GET"],
            desc="Get sticker filter options (categories/tags/bots)",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/file/<sticker_id>",
            view_handler=self.web_api.get_sticker_file,
            methods=["GET"],
            desc="Get sticker image file by sticker id",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/file/b64/<sticker_id>",
            view_handler=self.web_api.get_sticker_file_b64,
            methods=["GET"],
            desc="Get sticker image as base64 by sticker id",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/file/thumbnail/b64/<sticker_id>",
            view_handler=self.web_api.get_sticker_thumbnail_b64,
            methods=["GET"],
            desc="Get sticker thumbnail as base64 by sticker id",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/update",
            view_handler=self.web_api.update_sticker,
            methods=["POST"],
            desc="Update sticker metadata and bot ownership",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/delete",
            view_handler=self.web_api.delete_sticker,
            methods=["POST"],
            desc="Delete sticker with file and ownership cleanup",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/upload",
            view_handler=self.web_api.upload_stickers,
            methods=["POST"],
            desc="Upload sticker images manually (batch supported)",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/analyze",
            view_handler=self.web_api.analyze_sticker,
            methods=["POST"],
            desc="Re-run AI analysis for an existing sticker",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/batch",
            view_handler=self.web_api.batch_stickers,
            methods=["POST"],
            desc="Batch sticker operations",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/categories",
            view_handler=self.web_api.get_sticker_categories,
            methods=["GET"],
            desc="Get sticker category stats",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/categories/rename",
            view_handler=self.web_api.rename_sticker_category,
            methods=["POST"],
            desc="Rename or merge a sticker category",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/tags",
            view_handler=self.web_api.get_sticker_tags,
            methods=["GET"],
            desc="Get sticker tag stats",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/tags/rename",
            view_handler=self.web_api.rename_sticker_tag,
            methods=["POST"],
            desc="Rename or merge a sticker tag",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/tags/delete",
            view_handler=self.web_api.delete_sticker_tag,
            methods=["POST"],
            desc="Remove a tag from all stickers",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/gif_config",
            view_handler=self.web_api.get_sticker_gif_config,
            methods=["GET"],
            desc="Get per-bot send-sticker-as-gif switches",
        )
        ctx.register_web_api(
            route="/astrbot_plugin_giftia/stickers/gif_config",
            view_handler=self.web_api.set_sticker_gif_config,
            methods=["POST"],
            desc="Set per-bot send-sticker-as-gif switch",
        )
