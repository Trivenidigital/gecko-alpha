import pytest
from gecko.settings import GeckoSettings
from gecko.store import Store


@pytest.fixture
def settings(tmp_path) -> GeckoSettings:
    return GeckoSettings(db_path=str(tmp_path / "gecko.db"), _env_file=None)


@pytest.fixture
async def store(settings):
    s = await Store.open(settings.db_path)
    yield s
    await s.close()
