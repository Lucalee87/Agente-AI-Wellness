import pytest

from app.phone import normalize_phone


@pytest.mark.parametrize("raw,expected", [
    ("333 123 4567", "+393331234567"),
    ("+39 333 1234567", "+393331234567"),
    ("0039 333-123-4567", "+393331234567"),
    ("393331234567", "+393331234567"),
    ("081 1234567", "+390811234567"),
    ("+44 7911 123456", "+447911123456"),
    ("123", None),
    ("", None),
    (None, None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
