from __future__ import annotations


def american_to_decimal(american: float) -> float:
    if american == 0 or -100 < american < 100:
        raise ValueError(f"invalid American odds: {american}")
    if american > 0:
        return round(1 + american / 100, 6)
    return round(1 + 100 / -american, 6)


def decimal_to_american(decimal: float) -> int:
    if decimal <= 1:
        raise ValueError(f"invalid decimal odds: {decimal}")
    if decimal >= 2:
        return round((decimal - 1) * 100)
    return round(-100 / (decimal - 1))


def implied_probability(decimal: float) -> float:
    return 1 / decimal
