import time
from collections.abc import Sequence
from dataclasses import dataclass

from ..utils.schemas import MessageData


@dataclass
class ContextWindow:
    """Keep one active conversation's original history anchor."""

    start_id: str
    last_activity: float


class ContextWindows:
    def __init__(self, plugin):
        self.plugin = plugin
        self.windows: dict[tuple[str, str], ContextWindow] = {}

    def select(
        self,
        bot_name: str,
        session_id: str,
        messages: list[MessageData],
        reply_messages: Sequence[MessageData] = (),
    ) -> list[MessageData]:
        """Grow a frozen history from its initial anchor, then slide at the cap.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
            messages: Ordered snapshot excluding messages queued for later.
            reply_messages: Current targets that must fit in the initial window.

        Returns:
            The active period's visible messages, bounded by the configured cap.
        """
        if not messages:
            return []
        maximum = max(1, int(getattr(self.plugin, "msg_number", 200)))
        minimum = min(maximum, max(1, int(getattr(self.plugin, "min_msg_number", 20))))
        key = (bot_name, session_id)
        now = time.monotonic()
        window = self.windows.get(key)
        if (
            window
            and now - window.last_activity >= 300
            and not self.is_busy(bot_name, session_id)
        ):
            self.close(bot_name, session_id)
            window = None
        if window is None:
            start = max(0, len(messages) - minimum)
            targets = {msg.message_id for msg in reply_messages}
            start = next(
                (
                    min(start, i)
                    for i, msg in enumerate(messages)
                    if msg.message_id in targets
                ),
                start,
            )
        else:
            # An absent anchor has already slid out of the bounded snapshot.
            start = next(
                (
                    i
                    for i, msg in enumerate(messages)
                    if msg.message_id == window.start_id
                ),
                0,
            )
        selected = messages[start:][-maximum:]
        self.windows[key] = ContextWindow(selected[0].message_id, now)
        return selected

    def note_message(self, bot_name: str, session_id: str) -> bool:
        """Refresh activity, expiring a quiet period before admitting new input.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.

        Returns:
            Whether an old period ended before this message arrived.
        """
        window = self.windows.get((bot_name, session_id))
        if window is None:
            return False
        now = time.monotonic()
        if now - window.last_activity >= 300 and not self.is_busy(bot_name, session_id):
            self.close(bot_name, session_id)
            self.plugin.active_reply_counters[f"{bot_name}:{session_id}"] = 0
            return True
        window.last_activity = now
        return False

    def is_busy(self, bot_name: str, session_id: str) -> bool:
        """Protect a running decision, reply, or queued batch from idle expiry.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.

        Returns:
            Whether the conversation still has in-flight work.
        """
        if getattr(self.plugin, "replying_status", {}).get(
            f"{bot_name}:{session_id}", 0
        ):
            return True
        manager = getattr(self.plugin, "chat_manager", None)
        return manager is not None and any(
            state.task is not None and not state.task.done()
            for (owner, _), state in manager.sessions.items()
            if owner == bot_name
            and any(
                str(item.event.get_group_id() or item.event.get_sender_id())
                == str(session_id)
                for item in [*state.current, *state.pending]
            )
        )

    def close(self, bot_name: str, session_id: str) -> None:
        """Release a period without discarding stored or pending memory sources.

        Args:
            bot_name: Owning bot configuration.
            session_id: Owning conversation.
        """
        self.windows.pop((bot_name, session_id), None)
