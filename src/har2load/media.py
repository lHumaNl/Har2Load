"""Conservative MIME equivalence; opaque quoted values are never reordered."""

import re

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def mime_signature(value: str) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    parts = _parts(value)
    if parts is None:
        return None
    media = parts[0].strip().lower()
    if len(media.split("/")) != 2 or not all(_TOKEN.fullmatch(p) for p in media.split("/")):
        return None
    parameters: dict[str, str] = {}
    for part in parts[1:]:
        name, separator, raw = part.strip().partition("=")
        name, raw = name.strip().lower(), raw.strip()
        if not separator or not _TOKEN.fullmatch(name) or name in parameters:
            return None  # duplicate parameters are ambiguous, not equivalent by sorting
        parsed = _parameter(raw)
        if parsed is None:
            return None
        parameters[name] = parsed.lower() if name == "charset" else parsed
    return media, tuple(sorted(parameters.items()))


def _parameter(raw: str) -> str | None:
    if _TOKEN.fullmatch(raw):
        return raw
    if re.fullmatch(r'"(?:[^"\\\r\n]|\\[^\r\n])*"', raw):
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    return None


def _parts(value: str) -> list[str] | None:
    parts: list[str] = []
    start, quoted, escaped = 0, False, False
    for index, char in enumerate(value):
        if ord(char) < 32 and char != "\t":
            return None
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        return None
    parts.append(value[start:])
    return parts
