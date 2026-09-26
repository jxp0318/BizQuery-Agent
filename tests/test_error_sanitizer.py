"""P5.4 脱敏单测：异常映射与用户文案不含敏感细节。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.error_sanitizer import (
    DB_ERROR,
    INTERNAL_ERROR,
    LLM_ERROR,
    sanitize_exception,
)


class FakeOperationalError(Exception):
    pass


def test_db_error_hidden_from_user():
    err = FakeOperationalError(
        "(1045, \"Access denied for user 'shopkeeper'@'172.18.0.1' (using password: YES)\")"
    )
    safe = sanitize_exception(err)
    assert safe.code == DB_ERROR
    user = safe.for_user()
    assert "shopkeeper" not in user
    assert "172.18.0.1" not in user
    assert "password" not in user.lower()
    assert "不可用" in user


def test_timeout_maps_to_query_timeout():
    safe = sanitize_exception(TimeoutError("operation timed out"))
    assert safe.code == "query_timeout"


def test_llm_error():
    safe = sanitize_exception(RuntimeError("DeepSeek API authentication failed"))
    assert safe.code == LLM_ERROR


def test_unknown_maps_to_internal():
    safe = sanitize_exception(ValueError("boom"))
    assert safe.code == INTERNAL_ERROR
    assert "boom" not in safe.for_user()


if __name__ == "__main__":
    test_db_error_hidden_from_user()
    test_timeout_maps_to_query_timeout()
    test_llm_error()
    test_unknown_maps_to_internal()
    print("all ok")
