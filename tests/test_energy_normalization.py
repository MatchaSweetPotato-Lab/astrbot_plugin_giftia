from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.database.data_cache import DataCache
from core.utils.schemas import normalize_energy


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "100"),
        ("95.5", "96"),
        ("95.4%", "95"),
        (0, "0"),
        ("150", "100"),
        ("-2", "0"),
        ("invalid", "100"),
    ],
)
def test_normalize_energy(value, expected):
    assert normalize_energy(value) == expected


@pytest.mark.asyncio
async def test_data_cache_returns_integer_energy_from_lowest_read_layer():
    status = SimpleNamespace(energy="95.5", timestamp=0)
    cache = DataCache.__new__(DataCache)
    cache.bot_status = {}
    cache.db = SimpleNamespace(get_bot_status=AsyncMock(return_value=status))
    cache.energy_recovery_interval = 90

    result = await cache.get_bot_status("Giftia", "10001")

    assert result.energy == "96"
