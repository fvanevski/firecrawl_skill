import pytest
from unittest import mock

@pytest.fixture(autouse=True)
def mock_qdrant():
    with mock.patch("qdrant_client.QdrantClient"):
        yield
