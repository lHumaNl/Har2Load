"""Pure selection of initial native-jar cookies from the supplied capture snapshot.

Seeds are opt-in, not request headers or runtime extractors. Native consumers must
reset seeds with their logical iteration, never overwrite fresh cookies mid-iteration.
This module neither reads live state nor establishes native acceptance or parity.
"""

import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import IPv6Address
from urllib.parse import SplitResult, urlsplit

from .domain import Capture, Exchange, Har2LoadError, JSONValue

COOKIE_POLICY = "native_cookie_jar_v1"
_MAX_SELECTIONS = 256
_MAX_NAME = 256
_MAX_VALUE_BYTES = 65536
_MAX_DOMAIN = 253
_MAX_PATH = 4096
_SECONDS_PER_DAY = 86400
_ASCII_CONTROL_LIMIT = 32
_ASCII_DELETE = 127
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SELECTION = re.compile(r"([^:\s]+):/(request|response)/cookies/(0|[1-9][0-9]*)", re.ASCII)
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_EXPIRY = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", re.ASCII,
)
_INVALID = "INITIAL_COOKIE_INVALID"
_SELECTION_INVALID = "INITIAL_COOKIE_SELECTION_INVALID"
_CONFLICT = "INITIAL_COOKIE_CONFLICT"
_MESSAGES = {
    _INVALID: "Initial cookie is invalid.",
    _SELECTION_INVALID: "Initial cookie selection is invalid.",
    _CONFLICT: "Initial cookie selections conflict.",
}


@dataclass(frozen=True)
class NativeCookie:
    """Private seed data; same_site is lowercase metadata, not an enforcement claim."""

    name: str
    value: str
    domain: str
    path: str
    secure: bool
    http_only: bool
    expires: int | None
    domain_specified: bool
    path_specified: bool
    source_exchange_id: str
    source_area: str
    source_index: int
    same_site: str | None = None


def _error(code: str = _INVALID) -> Har2LoadError:
    return Har2LoadError(code, _MESSAGES[code], exit_code=3)


def plan_initial_cookies(capture: Capture, selections: tuple[str, ...]) -> tuple[NativeCookie, ...]:
    """Resolve only explicit rows in order; an empty selection does not inspect HAR.

    Missing secure conservatively inherits HTTPS, not unknown captured flags.
    Expired positive timestamps remain data; no live expiry filtering occurs.
    The caller supplies the revision-pinned capture; no model or raw HAR is changed."""
    if not isinstance(selections, tuple) or len(selections) > _MAX_SELECTIONS:
        raise _error(_SELECTION_INVALID)
    result: list[NativeCookie] = []
    selected: set[tuple[str, str, int]] = set()
    identities: set[tuple[str, str, str]] = set()
    for selection in selections:
        cookie = _resolve(capture, selection, selected)
        identity = (cookie.domain, cookie.name, cookie.path)
        if identity in identities:
            raise _error(_CONFLICT)
        identities.add(identity)
        result.append(cookie)
    return tuple(result)


def _parse_selection(selection: str) -> tuple[str, str, int]:
    match = _SELECTION.fullmatch(selection) if isinstance(selection, str) else None
    if match is None:
        raise _error(_SELECTION_INVALID)
    exchange_id, area, index = match.groups()
    # Bound integer conversion before handling potentially enormous index strings.
    return exchange_id, area, _bounded_index(index)


def _bounded_index(index: str) -> int:
    # A Python list cannot contain more than this many elements on supported hosts.
    maximum = str(sys.maxsize)
    if len(index) > len(maximum) or (len(index) == len(maximum) and index > maximum):
        raise _error(_SELECTION_INVALID)
    return int(index)


def _resolve(capture: Capture, selection: str,
             selected: set[tuple[str, str, int]]) -> NativeCookie:
    reference = _parse_selection(selection)
    if reference in selected:
        raise _error(_SELECTION_INVALID)
    selected.add(reference)
    exchange_id, area, index = reference
    matches = [exchange for exchange in capture.exchanges if exchange.id == exchange_id]
    if len(matches) != 1:
        raise _error(_SELECTION_INVALID)
    exchange = matches[0]
    row = _selected_row(exchange, area, index)
    url = _source_url(exchange)
    return _native_cookie(row, url, exchange_id, area, index)


def _selected_row(exchange: Exchange, area: str, index: int) -> dict[str, JSONValue]:
    raw = exchange.raw
    part = raw.get(area) if isinstance(raw, dict) else None
    rows = part.get("cookies") if isinstance(part, dict) else None
    if not isinstance(rows, list) or index >= len(rows):
        raise _error(_SELECTION_INVALID)
    row = rows[index]
    if not isinstance(row, dict):
        raise _error()
    return row


def _source_url(exchange: Exchange) -> SplitResult:
    raw = exchange.raw
    request = raw.get("request") if isinstance(raw, dict) else None
    value = request.get("url") if isinstance(request, dict) else None
    if not isinstance(value, str) or any(char.isspace() or _is_control(char) for char in value):
        raise _error()
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or url.username is not None:
            raise _error()
        _validate_hostname(url.hostname)
        _validate_authority(url)
        # Access forces urllib's port validation, without DNS or network access.
        _ = url.port
    except (ValueError, UnicodeError):
        raise _error() from None
    return url


def _validate_authority(url: SplitResult) -> None:
    host = url.hostname or ""
    authority_host = f"[{host}]" if ":" in host else host
    suffix = url.netloc.lower().removeprefix(authority_host)
    if suffix and (not suffix.startswith(":") or not suffix[1:].isascii()
                   or not suffix[1:].isdigit()):
        raise _error()


def _is_control(char: str) -> bool:
    return ord(char) < _ASCII_CONTROL_LIMIT or ord(char) == _ASCII_DELETE


def _validate_hostname(host: str | None) -> None:
    if not host or not host.isascii() or len(host) > _MAX_DOMAIN:
        raise _error()
    if ":" in host:
        if "%" in host:
            raise _error()
        IPv6Address(host)
        return
    labels = host.removesuffix(".").split(".")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise _error()


def _native_cookie(row: dict[str, JSONValue], url: SplitResult, exchange_id: str,
                   area: str, index: int) -> NativeCookie:
    return NativeCookie(
        name=_name(row.get("name")), value=_value(row.get("value")),
        domain=_domain(row, url.hostname or ""), path=_path(row, url.path),
        secure=_flag(row, "secure", url.scheme == "https"),
        http_only=_flag(row, "httpOnly", False), expires=_expires(row.get("expires")),
        domain_specified="domain" in row, path_specified="path" in row,
        source_exchange_id=exchange_id, source_area=area, source_index=index,
        same_site=_same_site(row),
    )


def _name(value: JSONValue) -> str:
    if not isinstance(value, str) or len(value) > _MAX_NAME or not _TOKEN.fullmatch(value):
        raise _error()
    return value


def _value(value: JSONValue) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _error()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise _error() from None
    if size > _MAX_VALUE_BYTES:
        raise _error()
    return value


def _domain(row: dict[str, JSONValue], host: str) -> str:
    if "domain" not in row:
        return host
    value = row["domain"]
    if not isinstance(value, str) or not value.isascii() or len(value) > _MAX_DOMAIN:
        raise _error()
    domain = value.removeprefix(".").lower()
    if domain != host:
        raise _error()
    return domain


def _path(row: dict[str, JSONValue], source_path: str) -> str:
    default = source_path.rpartition("/")[0] or "/"
    value = row.get("path", default)
    if (not isinstance(value, str) or not value.isascii() or not value.startswith("/")
            or len(value) > _MAX_PATH):
        raise _error()
    if any(_is_control(char) or char == ";" for char in value):
        raise _error()
    return value


def _flag(row: dict[str, JSONValue], key: str, default: bool) -> bool:
    value = row.get(key, default)
    if not isinstance(value, bool):
        raise _error()
    return value


def _expires(value: JSONValue) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _EXPIRY.fullmatch(value):
        raise _error()
    try:
        instant = datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        raise _error() from None
    delta = instant - _EPOCH
    seconds = delta.days * _SECONDS_PER_DAY + delta.seconds
    if delta.microseconds or seconds <= 0 or _has_subsecond_fraction(value):
        raise _error()
    return seconds


def _has_subsecond_fraction(value: str) -> bool:
    # fromisoformat truncates beyond microseconds; do not silently round those digits.
    fraction = re.search(r"\.([0-9]+)", value)
    return fraction is not None and any(char != "0" for char in fraction[1])


def _same_site(row: dict[str, JSONValue]) -> str | None:
    if "sameSite" not in row:
        return None
    value = row["sameSite"]
    if not isinstance(value, str) or value.lower() not in {"strict", "lax", "none"}:
        raise _error()
    return value.lower()
