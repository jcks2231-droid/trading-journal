import pytest

from signal_organizer import storage


@pytest.fixture
def conn():
    c = storage.connect(":memory:")
    yield c
    c.close()
