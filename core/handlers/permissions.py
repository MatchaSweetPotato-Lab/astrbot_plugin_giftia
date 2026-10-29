from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import AstrBotConfig
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.register.star_handler import get_handler_or_create
from astrbot.core.star.star_handler import EventType


class _SilentPermissionFilter(filter.CustomFilter):
    def __init__(self, permission: filter.PermissionTypeFilter):
        super().__init__(raise_error=False)
        self.permission = permission

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """Check the live command permission before parsing arguments.

        Args:
            event: Incoming command event.
            cfg: Active AstrBot configuration.

        Returns:
            Whether argument parsing and command matching may continue.
        """
        return self.permission.filter(event, cfg)


def admin_permission(handler):
    """Default a command to administrators and silently skip denied messages.

    Args:
        handler: Handler already registered with the command decorator.

    Returns:
        The original handler with configurable, silent permission checks.
    """
    filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)(handler)
    metadata = get_handler_or_create(handler, EventType.AdapterMessageEvent)
    permission = metadata.event_filters[-1]
    for command_filter in metadata.event_filters:
        if isinstance(command_filter, CommandFilter):
            # Reuse the native filter so dashboard permission overrides still apply.
            command_filter.add_custom_filter(_SilentPermissionFilter(permission))
    return handler
