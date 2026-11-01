"""Vet the optional selector (extra test arguments) a caller may add to a suite's command."""

from __future__ import annotations

import re
import shlex

from .config import Suite

_SAFE_VALUE = re.compile(r"^[\w\-./:\[\]=*,@+ ()]*$")
_TRAVERSAL = re.compile(r"(^|/)\.\.(/|$)")
_DRIVE = re.compile(r"^[A-Za-z]:(?!:)")  # C:foo, but not the node-id separator in file.py::test
MAX_TOKENS = 20
MAX_TOKEN_LENGTH = 200


class SelectorError(ValueError):
    pass


def parse_selector(selector: str | None, suite: Suite) -> list[str]:
    if not selector or not selector.strip():
        return []
    if not suite.allow_selector:
        raise SelectorError(f"Suite '{suite.name}' does not accept a selector")
    if "\\" in selector:  # shlex would silently drop backslashes and turn C:\x into the relative path C:x
        raise SelectorError("Backslashes are not allowed in a selector. Use forward slashes.")
    try:
        tokens = shlex.split(selector)
    except ValueError as exc:
        raise SelectorError(f"Could not parse selector: {exc}") from exc
    if len(tokens) > MAX_TOKENS:
        raise SelectorError(f"Selector has more than {MAX_TOKENS} parts")
    for token in tokens:
        if len(token) > MAX_TOKEN_LENGTH:
            raise SelectorError("A selector part is too long")
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag not in suite.allowed_flags:
                raise SelectorError(
                    f"Option '{flag}' is not allowed. Allowed: {', '.join(suite.allowed_flags) or 'none'}")
            token = token.split("=", 1)[1] if "=" in token else ""
        if not _SAFE_VALUE.match(token):
            raise SelectorError(f"Unsupported characters in selector part '{token[:40]}'")
        if token.startswith("/") or _DRIVE.match(token) or _TRAVERSAL.search(token):
            raise SelectorError("Absolute paths, drive letters and '..' are not allowed in a selector")
    return tokens
