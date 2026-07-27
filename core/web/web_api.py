from .bot_status_api import BotStatusApi
from .chat_history_api import ChatHistoryApi
from .forward_api import ForwardApi
from .media_api import MediaApi
from .memory_api import MemoryApi
from .profile_api import ProfileApi
from .task_api import TaskApi
from .token_api import TokenApi


class GiftiaWebApi(
    MediaApi,
    ForwardApi,
    TokenApi,
    ChatHistoryApi,
    MemoryApi,
    BotStatusApi,
    TaskApi,
    ProfileApi,
):
    """Giftia plugin web APIs for dashboard pages.

    Facade that aggregates all API domains via multiple inheritance.
    """

    pass
