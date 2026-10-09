import pytest
from gecko.settings import GeckoSettings


@pytest.fixture
def settings(tmp_path) -> GeckoSettings:
    return GeckoSettings(db_path=str(tmp_path / "gecko.db"), _env_file=None)
