import pytest

from app.zotero_service import ZoteroService
from tests.fakes import FakeZotero


@pytest.fixture
def fake_zot() -> FakeZotero:
    return FakeZotero()


@pytest.fixture
def service(fake_zot: FakeZotero) -> ZoteroService:
    return ZoteroService(fake_zot, api_key="fake-key")
