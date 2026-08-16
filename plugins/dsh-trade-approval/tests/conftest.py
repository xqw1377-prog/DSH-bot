import pytest

from quant_gateway import approval_store


@pytest.fixture(autouse=True)
def reset_gateway_state():
    approval_store.reset()
    yield
    approval_store.reset()
