"""Conservative native cookie API construction, never a jar or runtime extractor."""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from har2load.cookies import NativeCookie
from har2load.domain import Diagnostic

DOC_URLS = (
    "https://admhelp.microfocus.com/vugen/en/26.1/help/function_reference/Content/FuncRef/"
    "web/lrFR_web_add_cookie.htm",
    "https://admhelp.microfocus.com/vugen/en/26.1/help/function_reference/Content/FuncRef/"
    "web/lrFR_web_cleanup_cookies.htm",
)
INSTRUCTIONS = """
Cookie policy: native_cookie_jar_v1. Each Action iteration requests cookie cleanup,
then cache cleanup, then adds only explicitly selected initial cookies before HTTP.
No selection emits no web_add_cookie calls. No cleanup or reseeding occurs between
requests, leaving fresh server cookies to the native jar. Captured Cookie headers
are not replayed. Seed literals in Action.c are private/unredacted.
With seeds, cookie cleanup/add failures log value-free lr_output_message diagnostics
and continue without overriding transaction status. Cleanup failure does not prove
an empty jar; subsequent seed additions and HTTP calls are still attempted. Native
API failures may have their own native effects; those effects remain unverified.
Without seeds, cleanup return values remain unchecked. Cache returns are unchecked.
Only explicit Domain seeds are supported: web_add_cookie requires Domain and has no
documented origin-bound host-only equivalent. SameSite seeds are blocked. HttpOnly
is retained as model metadata but not emitted: no JavaScript context is generated,
and native HttpOnly enforcement is unverified. Expiry is a deterministic UTC date,
never extended or changed to session lifetime; past dates are documented deletions.
Empty values are emitted literally, not interpreted as deletion by this generator.
Domain/path/secure/date behavior and empty-value acceptance need actual SDK testing.
The 26.1 cookie API references are construction facts, not a verified target version.
"""
_PREFIX = "LOADRUNNER_INITIAL_COOKIE_"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_EXPIRY = 253402300799  # Last whole UTC second representable by datetime.
_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", re.ASCII)
# RFC 6265 cookie-octet; braces are additionally rejected for native expansion.
_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{0,65536}", re.ASCII)
_DOMAIN = re.compile(r"[a-z0-9.-]{1,253}", re.ASCII)
_PATH = re.compile(r'/[\x20-\x21\x23-\x3a\x3c-\x5b\x5d-\x7e]{0,4095}', re.ASCII)


def _error(suffix: str, message: str) -> Diagnostic:
    return Diagnostic(_PREFIX + suffix, "error", message, target="loadrunner")


def _representation_valid(cookie: NativeCookie) -> bool:
    return all(pattern.fullmatch(value) is not None for pattern, value in (
        (_NAME, cookie.name), (_VALUE, cookie.value),
        (_DOMAIN, cookie.domain), (_PATH, cookie.path)))


def diagnostics(cookies: tuple[NativeCookie, ...]) -> Iterator[Diagnostic]:
    """Check native representability after shared immutable selection validation."""
    for cookie in cookies:
        if not cookie.domain_specified:
            yield _error("HOST_ONLY_UNSUPPORTED", "Native host-only cookie scope is unsupported.")
        if cookie.same_site is not None:
            yield _error("SAME_SITE_UNSUPPORTED", "Native SameSite enforcement is unsupported.")
        if any("{" in value or "}" in value for value in
               (cookie.name, cookie.value, cookie.domain, cookie.path)):
            yield _error("METASYNTAX_UNSUPPORTED", "Native cookie expansion is unsupported.")
        if not _representation_valid(cookie):
            yield _error("REPRESENTATION_UNSUPPORTED", "Native cookie literal is unsupported.")
        if cookie.expires is not None and (type(cookie.expires) is not int or
                                           not 0 < cookie.expires <= _MAX_EXPIRY):
            yield _error("EXPIRY_UNSUPPORTED", "Native cookie expiry is unsupported.")


def _argument(cookie: NativeCookie) -> str:
    parts = [cookie.name + "=" + cookie.value, "domain=" + cookie.domain, "path=" + cookie.path]
    if cookie.expires is not None:
        parts.append("expires=" + format_datetime(_EPOCH + timedelta(seconds=cookie.expires),
                                                  usegmt=True))
    if cookie.secure:
        parts.append("secure")
    return "; ".join(parts)


def action_lines(cookies: tuple[NativeCookie, ...], literal: Callable[[str], str]) -> Iterator[str]:
    """Reset once per Action, never replay initial state between native requests."""
    if cookies:
        yield "    if (web_cleanup_cookies() != LR_PASS)"
        yield f'        lr_output_message("{_PREFIX}CLEANUP_FAILED");'
    else:
        yield "    web_cleanup_cookies();"
    yield "    web_cache_cleanup();"
    for cookie in cookies:
        yield f"    if (web_add_cookie({literal(_argument(cookie))}) != LR_PASS)"
        yield f'        lr_output_message("{_PREFIX}ADD_FAILED");'
