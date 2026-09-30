"""Decode the body of a Bash $'...' string the way the shell does."""

from __future__ import annotations

SIMPLE = {"a": 7, "b": 8, "e": 27, "E": 27, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, "'": 39, '"': 34,
          "?": 63}
DIGITS = {"x": ("0123456789abcdefABCDEF", 2), "u": ("0123456789abcdefABCDEF", 4), "U": ("0123456789abcdefABCDEF", 8)}


def decode(body: str) -> str:
    """The text a shell builds from the body of $'...'; an escape that yields NUL ends it, as in a C string."""
    out = bytearray()
    i, n = 0, len(body)
    while i < n:
        char = body[i]
        if char != "\\" or i + 1 >= n:
            out += char.encode("utf-8", "replace")
            i += 1
            continue
        kind = body[i + 1]
        i += 2
        if kind in SIMPLE:
            out.append(SIMPLE[kind])
        elif kind in "01234567":
            j = i
            while j < n and j < i + 2 and body[j] in "01234567":
                j += 1
            value = int(body[i - 1:j], 8) & 0xFF
            i = j
            if value == 0:
                break
            out.append(value)
        elif kind in DIGITS:
            alphabet, width = DIGITS[kind]
            j = i
            while j < n and j < i + width and body[j] in alphabet:
                j += 1
            if j == i:
                out += b"\\" + kind.encode()
                continue
            value = int(body[i:j], 16)
            i = j
            if value == 0:
                break
            if kind == "x":
                out.append(value)
            elif value <= 0x10FFFF:
                out += chr(value).encode("utf-8", "replace")
        elif kind == "c" and i < n:
            value = 0x7F if body[i] == "?" else ord(body[i]) & 0x1F
            i += 1
            if value == 0:
                break
            out.append(value)
        else:
            out += b"\\" + kind.encode("utf-8", "replace")
    return out.decode("utf-8", "replace")


def quoted(body: str) -> str:
    """The decoded body as a single-quoted shell word."""
    return "'" + decode(body).replace("'", "'\\''") + "'"
