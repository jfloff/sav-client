"""
Utility helpers for SavClient.

Kept intentionally thin — only things used in more than one place live here.
"""

from __future__ import annotations

import hashlib
import re


def md5_hex(value: str) -> str:
    """
    Return the lowercase hex MD5 digest of a UTF-8 string.

    This replicates the client-side `CryptoJS.MD5(pass).toString()` call that
    the SAV login page performs before POSTing credentials.
    """
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    """Remove HTML tags and normalise common break tags to newlines."""
    text = re.sub(r"<br\s*/?>|</br>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
