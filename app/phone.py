"""Normalizzazione dei numeri di telefono (default: Italia)."""

import re


def normalize_phone(raw: str | None, default_prefix: str = "+39") -> str | None:
    """Restituisce il numero in formato E.164 (+39...) oppure None se non valido."""
    if not raw:
        return None
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits, plus = digits[2:], True
    if not digits:
        return None
    if plus:
        number = "+" + digits
    elif digits.startswith("39") and len(digits) >= 11:
        number = "+" + digits
    else:
        number = default_prefix + digits
    # E.164: massimo 15 cifre; un numero italiano ha almeno 9 cifre dopo il +39
    if not 10 <= len(number) - 1 <= 15:
        return None
    return number
