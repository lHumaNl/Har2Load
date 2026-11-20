"""Read an explicit, source-pinned composition decision, never infer equivalence or consent."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from har2load.domain import Har2LoadError, JSONValue
from har2load.serialization import metadata_object
from har2load.workspace import read_limited

TF_LABEL = re.compile(r"TF_[A-Za-z0-9][A-Za-z0-9_.-]{0,59}")
_GROUP = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_LIMIT = 256 * 1024


@dataclass(frozen=True)
class StepUse:
    uc: str
    manifest_sha256: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class SharedBlock:
    name: str
    uses: tuple[StepUse, ...]


@dataclass(frozen=True)
class SharedPlan:
    blocks: tuple[SharedBlock, ...]
    digest: str
    document: dict[str, JSONValue]


def _invalid() -> Har2LoadError:
    return Har2LoadError("COMPOSE_SHARED_PLAN_INVALID",
                         "Expected a bounded shared-step plan with explicit decisions.", 2)


def _record(value: JSONValue, keys: set[str]) -> dict[str, JSONValue]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid()
    return value


def _items(value: JSONValue, low: int, high: int) -> list[JSONValue]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise _invalid()
    return value


def _use(value: JSONValue, sources: dict[str, str], ordinal: int) -> StepUse:
    item = _record(value, {"uc", "manifest_sha256", "groups"})
    uc, pin = item["uc"], item["manifest_sha256"]
    if not isinstance(uc, str) or uc not in sources or not isinstance(pin, str):
        raise _invalid()
    if sources[uc] != pin:
        raise Har2LoadError("COMPOSE_SHARED_STALE",
                            f"Shared block #{ordinal}: source manifest pin does not match.", 3)
    groups = _items(item["groups"], 1, 128)
    if not all(isinstance(g, str) and _GROUP.fullmatch(g) for g in groups):
        raise _invalid()
    return StepUse(uc, pin, tuple(cast(list[str], groups)))


def read_shared_plan(path: Path, sources: dict[str, str]) -> SharedPlan:
    try:
        raw = read_limited(path, _LIMIT, private=True)
        doc = metadata_object(raw)
    except Har2LoadError:
        raise _invalid() from None
    _record(doc, {"schema_version", "blocks"})
    if doc["schema_version"] != "1.0":
        raise _invalid()
    names = {name.lower() for name in sources}
    occupied: set[tuple[str, str]] = set()
    blocks = []
    for ordinal, value in enumerate(_items(doc["blocks"], 1, 32), 1):
        item = _record(value, {"name", "decision", "reason", "uses"})
        name, reason = item["name"], item["reason"]
        if (not isinstance(name, str) or not TF_LABEL.fullmatch(name) or name.lower() in names
                or item["decision"] not in ("high_confidence", "user_confirmed")
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 1024
                or any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in reason)):
            raise _invalid()
        names.add(name.lower())
        uses = tuple(_use(v, sources, ordinal) for v in _items(item["uses"], 2, len(sources)))
        if len({u.uc for u in uses}) != len(uses):
            raise _invalid()
        for u in uses:
            for group in u.groups:
                if (u.uc, group) in occupied:
                    raise _invalid()
                occupied.add((u.uc, group))
        blocks.append(SharedBlock(name, uses))
    return SharedPlan(tuple(blocks), hashlib.sha256(raw).hexdigest(), doc)
