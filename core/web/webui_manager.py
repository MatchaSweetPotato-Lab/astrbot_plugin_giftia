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
