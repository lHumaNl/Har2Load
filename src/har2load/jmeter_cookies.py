"""Native CookieManager seeds, not fixed headers or custom runtime cookie state."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from har2load.cookies import NativeCookie

MAX_INITIAL_COOKIES = 256
_TOKEN = re.compile(r"[!#%&'*+.^_`|~0-9A-Za-z-]+\Z")
_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_UNSUPPORTED = "JMETER_INITIAL_COOKIE_UNSUPPORTED"
SOURCE_BASE = "https://raw.githubusercontent.com/apache/jmeter/7153c74dd0e860d0ca3825cc5a23e976e0242b6e/src/"
SOURCES = [SOURCE_BASE + path for path in (
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/control/Cookie.java",
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/control/CookieManager.java",
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/control/HC4CookieHandler.java",
    "protocol/http/src/main/java/org/apache/jmeter/protocol/http/gui/CookiePanel.java",
    "core/src/main/java/org/apache/jmeter/engine/util/ValueReplacer.java",
)]
LIMITATIONS = [
    "Only explicitly selected session cookies seed the native jar; default is empty.",
    "Native main-iteration reset restores initial seeds, not per-request captured cookies.",
    "Explicit expiry and SameSite seeds are blocked: native enforcement is unsupported.",
    "IPv6 cookie seeds are blocked: normalized domains do not match native bracketed URL hosts.",
    "HttpOnly metadata has no native cookie field or JavaScript context in HTTP samplers; "
    "no HttpOnly enforcement is claimed.",
    "CookiePanel GUI edits lose host-only/path flags and expiry; preserve generated XML.",
]


class CookieRejected(Exception):
    """Only fixed, value-free capability diagnostics cross the target boundary."""


def strings(cookies: tuple[NativeCookie, ...]) -> Iterator[str]:
    for cookie in cookies:
        yield from (cookie.name, cookie.value, cookie.domain, cookie.path)


def validate(cookies: tuple[NativeCookie, ...]) -> None:
    if len(cookies) > MAX_INITIAL_COOKIES:
        raise CookieRejected(_UNSUPPORTED)
    for cookie in cookies:
        # HC4's manual seed match does not filter expiry; never change it into a session cookie.
        if cookie.expires is not None:
            raise CookieRejected("JMETER_INITIAL_COOKIE_EXPIRY_UNSUPPORTED")
        if cookie.same_site is not None:
            raise CookieRejected("JMETER_INITIAL_COOKIE_SAMESITE_UNSUPPORTED")
        _validate_literal(cookie)


def _validate_literal(cookie: NativeCookie) -> None:
    # ValueReplacer and Cookie both interpret functions; XML escaping is not enough.
    for text in strings((cookie,)):
        if any(not " " <= char <= "~" or char in "$\\" for char in text):
            raise CookieRejected(_UNSUPPORTED)
    if not _TOKEN.fullmatch(cookie.name) or not _VALUE.fullmatch(cookie.value):
        raise CookieRejected(_UNSUPPORTED)
    if not cookie.path.startswith("/") or ";" in cookie.path or not _domain(cookie.domain):
        raise CookieRejected(_UNSUPPORTED)


def _domain(value: str) -> bool:
    # HC4 uses Java URL.getHost(), including IPv6 brackets; do not silently emit unmatched seeds.
    return all(_HOST_LABEL.fullmatch(label) for label in value.removesuffix(".").split("."))


def attach(collection: ET.Element, cookies: tuple[NativeCookie, ...]) -> None:
    for index, cookie in enumerate(cookies):
        # SaveService maps testname to TestElement.name; Cookie.name is not a native property.
        node = ET.SubElement(collection, "elementProp", name=str(index), elementType="Cookie",
                             testname=cookie.name)
        for field in ("value", "domain", "path"):
            ET.SubElement(node, "stringProp", name="Cookie." + field).text = getattr(cookie, field)
        for field in ("secure", "domain_specified", "path_specified"):
            ET.SubElement(node, "boolProp", name="Cookie." + field).text = str(
                getattr(cookie, field)).lower()
        ET.SubElement(node, "longProp", name="Cookie.expires").text = "0"
        ET.SubElement(node, "intProp", name="Cookie.version").text = "0"
