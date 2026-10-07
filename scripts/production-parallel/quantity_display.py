"""Employee-facing quantity labels; never used for parsing or ledger routing."""

import re


def quantity_note(worker):
    """Keep A/B wording free of the other process name for safe copy/paste."""
    if worker in ('A', 'B'):
        return '数量口径：**下机翻袜产量**（已翻好数量）'
    if worker in ('C', 'D'):
        return '数量口径：**烤边产量**'
    return ''


def summary_quantity_note(summary):
    """Read only the identity line already inserted by the review pipeline.

    This is a display hint, not employee identification. Ambiguous or unknown
    identities receive no label; the stored summary and its digest stay intact.
    """
    workers = set(re.findall(
        r'(?m)^(?:身份|老板代报|员工代号)：([ABCD])(?:=|｜|（)', summary))
    return quantity_note(next(iter(workers))) if len(workers) == 1 else ''
