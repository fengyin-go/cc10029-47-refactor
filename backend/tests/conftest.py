"""测试公共夹具：每个用例开始前清空派发回执台账。

各用例会用 ``clear_all`` 清空业务数据，回执台账是独立的内存结构，这里统一
重置，避免上一个用例的源单 ID 影响下一个用例。
"""
from __future__ import annotations

import pytest

from app.services.relay import relay_ledger


@pytest.fixture(autouse=True)
def _reset_relay_ledger():
    relay_ledger.clear()
    yield
    relay_ledger.clear()
