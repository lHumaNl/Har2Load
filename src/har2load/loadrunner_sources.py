"""Pure construction of native parameter assets and bounded scalar C sources."""

import re
from collections.abc import Iterator
from datetime import date
from hashlib import sha256
from typing import cast

from har2load.domain import Diagnostic, JSONValue
from har2load.runtime_sources import initial_values, source_diagnostics
from har2load.runtime_types import MAX_RUNTIME_VALUE_BYTES, RuntimePlan

PARAMETER_FILE = "loadrunner/native_inputs.prm"
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_INTEGER_BIAS = 2**63
SOURCE_URLS = (
    "https://github.com/richardbishop/LoadRunnerSamples/blob/"
    "79026c4b4e2efe9e481b945820700dae83f5ab25/MySQL/WritetoMySQL/WritetoMySQL.prm",
    "https://github.com/POHNAH/HW_NT_5_4/blob/"
    "af4e32a5bf8e304d16106cb73e74688d89d52375/HW_NT_5_4.prm",
    "https://admhelp.microfocus.com/vugen/en/26.1-26.3/help/WebHelp/Content/VuGen/"
    "UI/116100_ui_parameters_property_db.htm",
    "https://admhelp.microfocus.com/vugen/en/26.1-26.3/help/WebHelp/Content/VuGen/"
    "104400_r_files_generated_during_recording.htm",
)
INSTRUCTIONS = """
Native inputs (native_initial_snapshot_v1): project assets, not a custom loader.
Copy native_inputs.prm and data/ together into a real, verified VuGen project.
Merge the fragment into its parameter file, or set [General]
ParameterFile=native_inputs.prm in the real project's .usr. Preserve other settings.
If no supplied inputs exist, no fragment/data files are emitted; skip that step.
Review the Parameters UI: Table, Value column, local data file, update Once,
Sequential, first data row 1, ContinueWithLast. Never enable AbortVuser here.
Known initial values are private/unredacted. No JSON loader or environment lookup.
Only declared keys with supplied initials are evaluated, once at session start.
Changing those native UI values affects that snapshot; no source initial fallback.
Adding an undefined key requires a model initial declaration and regeneration.
Missing declarations produce markers without evaluating unknown native parameters.
Initial .dat capability: nonempty printable ASCII, no quotes/comma, no edge spaces.
Empty, Unicode, multiline, delimiter and quoting representations are blocked rather
than silently changed. Native UI edits remain subject to runtime UTF-8/type limits.
The SDK's missing/corrupt native-file behavior and parameter presence are unverified.
Broken projects can fail before script startup; generated code cannot contain this.
Literal ERROR_* and brace-shaped strings are data, not missing-value sentinels.
Generators use per-Vuser static SplitMix64 state seeded at native session startup
with lr_whoami, time and clock. No crypto, uniqueness, identical-stream, concurrent
isolation or native ABI guarantees. C99 uint64_t/int64_t support and a Unix-seconds
time_t implementation require verification in the selected native toolchain.
UTC arithmetic is bounded Gregorian year 1..9999, without shared gmtime buffers.
Native asset construction references (not a verified file-format specification):
""" + "\n".join(SOURCE_URLS) + "\n"

_EXTERNAL = r'''
static void h2l_native_snapshot(h2l_value *out, const char *expression, int length)
{
    char *native = NULL; const char *owned; unsigned long n = 0; int rc;
    h2l_reset(out);
    rc = lr_eval_string_ext(expression, length, &native, &n, 0, 0, -1);
    if (rc == LR_PASS && native) h2l_set(out, native, n, 1, 0);
    else h2l_error(out);
    /* The documented eval contract requires cleanup even for a NULL result. */
    owned = native; lr_eval_string_ext_free(&owned);
}
static void h2l_external(h2l_value *out, const h2l_value *snapshot, int type, int empty)
{
    h2l_json document; h2l_value parsed = { NULL, out->error, 0, 0 };
    h2l_reset(out);
    if (!snapshot->data || (!empty && !*snapshot->data)) { h2l_error(out); return; }
    if (type == 1) { h2l_accept(out, snapshot, type); return; }
    h2l_json_parse(&document, snapshot->data);
    h2l_json_select(&parsed, &document, NULL, 0, 1, 1);
    h2l_accept(out, &parsed, type);
    h2l_reset(&parsed); h2l_json_free(&document);
}
'''

_GENERATORS = r'''
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <time.h>
#define H2L_RANDOM_ATTEMPTS 128
#define H2L_GENERATED_LENGTH 4096
#define H2L_DAY_SECONDS INT64_C(86400)
#define H2L_EPOCH_ORDINAL INT64_C(719163)
#define H2L_LAST_ORDINAL INT64_C(3652059)
#define H2L_FIRST_SECOND (-INT64_C(62135596800))
#define H2L_LAST_SECOND INT64_C(253402300799)
/* SplitMix64's published mixing constants; unsigned wraparound is intentional. */
static uint64_t h2l_random_state;
static int h2l_random_ready;
static uint64_t h2l_random_next(void)
{
    uint64_t z = (h2l_random_state += UINT64_C(0x9e3779b97f4a7c15));
    z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
    return z ^ (z >> 31);
}
static void h2l_random_start(void)
{
    int id = 0; time_t now = time(NULL); clock_t ticks = clock();
    lr_whoami(&id, NULL, NULL);
    h2l_random_ready = now != (time_t)-1 && ticks != (clock_t)-1;
    h2l_random_state = 0;
    if (!h2l_random_ready) return;
    h2l_random_state = (uint64_t)now ^ ((uint64_t)(unsigned int)id << 32) ^ (uint64_t)ticks;
}
static int h2l_random_bounded(uint64_t span, uint64_t *out)
{
    unsigned int attempt; uint64_t value, threshold = span ? (UINT64_C(0) - span) % span : 0;
    if (!h2l_random_ready) return 0;
    for (attempt = 0; attempt < H2L_RANDOM_ATTEMPTS; ++attempt) {
        value = h2l_random_next();
        if (value >= threshold) { *out = span ? value % span : value; return 1; }
    }
    return 0;
}
static void h2l_generate_integer(h2l_value *out, uint64_t low, uint64_t high)
{
    uint64_t rank; int64_t value; char text[32]; int length;
    h2l_reset(out);
    if (!h2l_random_bounded(high - low + UINT64_C(1), &rank)) { h2l_error(out); return; }
    rank += low;
    value = rank <= (uint64_t)INT64_MAX ? INT64_MIN + (int64_t)rank :
            (int64_t)(rank - (UINT64_C(1) << 63));
    length = snprintf(text, sizeof(text), "%" PRId64, value);
    if (length < 0 || (size_t)length >= sizeof(text)) { h2l_error(out); return; }
    h2l_set(out, text, (size_t)length, 2, 1);
}
static void h2l_generate_string(h2l_value *out, size_t length, const char *alphabet)
{
    char text[H2L_GENERATED_LENGTH + 1]; size_t i, count = strlen(alphabet); uint64_t index;
    h2l_reset(out);
    if (!count || !length || length > H2L_GENERATED_LENGTH) { h2l_error(out); return; }
    for (i = 0; i < length; ++i) {
        if (!h2l_random_bounded(count, &index)) { h2l_error(out); return; }
        text[i] = alphabet[index];
    }
    h2l_set(out, text, length, 1, 0);
}
static void h2l_generate_uuid(h2l_value *out)
{
    char text[36]; const char *hex = "0123456789abcdef"; size_t i; uint64_t digit;
    h2l_reset(out);
    for (i = 0; i < sizeof(text); ++i) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { text[i] = '-'; continue; }
        if (!h2l_random_bounded(16, &digit)) { h2l_error(out); return; }
        if (i == 14) digit = 4;
        if (i == 19) digit = (digit & 3) | 8;
        text[i] = hex[digit];
    }
    h2l_set(out, text, sizeof(text), 1, 0);
}
static int h2l_days_before_year(int year)
{ int y = year - 1; return 365 * y + y / 4 - y / 100 + y / 400; }
static int h2l_date_text(char *out, size_t capacity, int ordinal)
{
    static const int months[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    int low = 1, high = 10000, middle, month = 1, days, length, leap;
    if (ordinal < 1 || ordinal > H2L_LAST_ORDINAL) return 0;
    while (low + 1 < high) {
        middle = low + (high - low) / 2;
        if (h2l_days_before_year(middle) < ordinal) low = middle; else high = middle;
    }
    ordinal -= h2l_days_before_year(low);
    leap = low % 4 == 0 && (low % 100 != 0 || low % 400 == 0);
    while (month < 12) {
        days = months[month - 1] + (month == 2 && leap);
        if (ordinal <= days) break;
        ordinal -= days; ++month;
    }
    length = snprintf(out, capacity, "%04d-%02d-%02d", low, month, ordinal);
    return length == 10 && (size_t)length < capacity;
}
static void h2l_generate_date(h2l_value *out, int low, int high)
{
    uint64_t offset; char text[11];
    h2l_reset(out);
    if (!h2l_random_bounded((uint64_t)(high - low + 1), &offset) ||
        !h2l_date_text(text, sizeof(text), low + (int)offset)) { h2l_error(out); return; }
    h2l_set(out, text, 10, 1, 0);
}
static int h2l_utc_seconds(int64_t offset, int64_t *out)
{
    time_t now = time(NULL); double seconds;
    if (now == (time_t)-1) return 0;
    /* Selected toolchain must use Unix-seconds time_t; no shared libc calendar state. */
    seconds = difftime(now, (time_t)0);
    if (!(seconds >= H2L_FIRST_SECOND && seconds <= H2L_LAST_SECOND)) return 0;
    *out = (int64_t)seconds + offset;
    return *out >= H2L_FIRST_SECOND && *out <= H2L_LAST_SECOND;
}
static void h2l_generate_datetime(h2l_value *out, int64_t offset)
{
    int64_t seconds, day, remainder; char text[21]; int length;
    h2l_reset(out);
    if (!h2l_random_ready || !h2l_utc_seconds(offset, &seconds)) { h2l_error(out); return; }
    day = seconds / H2L_DAY_SECONDS; remainder = seconds % H2L_DAY_SECONDS;
    if (remainder < 0) { --day; remainder += H2L_DAY_SECONDS; }
    if (!h2l_date_text(text, sizeof(text), (int)(day + H2L_EPOCH_ORDINAL))) {
        h2l_error(out); return;
    }
    length = snprintf(text + 10, sizeof(text) - 10, "T%02d:%02d:%02dZ",
                      (int)(remainder / 3600), (int)(remainder / 60 % 60), (int)(remainder % 60));
    if (length != 10) { h2l_error(out); return; }
    h2l_set(out, text, 20, 1, 0);
}
'''


def _source(variable: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return cast(dict[str, JSONValue], variable["source"])


def _keys(plan: RuntimePlan) -> list[str]:
    return sorted({cast(str, _source(v)["key"]) for v in plan.variables
                   if _source(v)["kind"] == "external"})


def _generated(plan: RuntimePlan) -> bool:
    return any(_source(v)["kind"] == "generated" for v in plan.variables)


def diagnostics(plan: RuntimePlan) -> list[Diagnostic]:
    errors = source_diagnostics(plan, "loadrunner")
    for variable in plan.variables:
        source = _source(variable)
        if source["kind"] != "external":
            continue
        key, value = cast(str, source["key"]), source.get("initial_value")
        if (not _KEY.fullmatch(key) or key == "H2L_RESPONSE" or
                (value is not None and not _safe_initial(cast(str, value)))):
            errors.append(Diagnostic("LOADRUNNER_NATIVE_INPUT_REPRESENTATION_UNSUPPORTED", "error",
                "Native file input requires a safe key and nonempty single-line ASCII value; "
                "quotes, delimiters, edge spaces and the capture-reserved key are unsupported.",
                entity_id=cast(str, variable["id"]), target="loadrunner"))
    return errors


def _safe_initial(value: str) -> bool:
    return 0 < len(value) <= MAX_RUNTIME_VALUE_BYTES and value.strip() == value and all(
        " " <= c <= "~" and c not in '\",' for c in value)


def data_path(key: str) -> str:
    return "data/input_" + sha256(key.encode("utf-8")).hexdigest() + ".dat"


def _parameter(key: str) -> list[str]:
    return [f"[parameter:{key}]", f'ParamName="{key}"', 'Type="Table"',
            f'Table="{data_path(key)}"', 'ColumnName="Value"', 'TableLocation="Local"',
            'Delimiter=","', 'GenerateNewVal="Once"', 'SelectNextRow="Sequential"',
            'StartRow="1"', 'OutOfRangePolicy="ContinueWithLast"', 'OriginalValue=""',
            'auto_allocate_block_size="1"', 'value_for_each_vuser=""']


def artifacts(plan: RuntimePlan) -> Iterator[tuple[str, list[str]]]:
    seeds = initial_values(plan)
    if seeds:
        yield PARAMETER_FILE, [line for key in seeds for line in _parameter(key)]
    for key, value in seeds.items():
        yield "loadrunner/" + data_path(key), ["Value", value]


def declarations(plan: RuntimePlan) -> Iterator[str]:
    from har2load.loadrunner_runtime import symbol
    if _keys(plan):
        yield from _EXTERNAL.splitlines()
    if _generated(plan):
        yield from _GENERATORS.splitlines()
    for key in _keys(plan):
        yield f'static h2l_value {symbol("INPUT", key)} = {{ NULL, "ERROR_NATIVE_INPUT", 0, 0 }};'


def session_lines(plan: RuntimePlan, start: bool) -> Iterator[str]:
    from har2load.loadrunner_runtime import literal, symbol
    seeds = initial_values(plan)
    for key in _keys(plan):
        name = symbol("INPUT", key)
        yield f"    h2l_reset(&{name});"
        if start and key in seeds:
            expression = "{" + key + "}"
            yield f"    h2l_native_snapshot(&{name}, {literal(expression)}, {len(expression)});"
    if _generated(plan):
        yield ("    h2l_random_start();" if start else
               "    h2l_random_ready = 0; h2l_random_state = 0;")


def initializer(variable: dict[str, JSONValue], name: str, kind: int) -> Iterator[str]:
    from har2load.loadrunner_runtime import symbol
    source = _source(variable)
    if source["kind"] == "external":
        snapshot = symbol("INPUT", cast(str, source["key"]))
        empty = int(bool(source["allow_empty"]))
        yield f"    h2l_external({name}(), &{snapshot}, {kind}, {empty});"
    else:
        yield f"    {_generator_call(source, name + '()')};"


def _generator_call(source: dict[str, JSONValue], out: str) -> str:
    from har2load.loadrunner_runtime import literal
    generator = source["generator"]
    if generator == "integer":
        bounds = [f"UINT64_C({cast(int, source[k]) + _INTEGER_BIAS})"
                  for k in ("minimum", "maximum")]
        return f"h2l_generate_integer({out}, {', '.join(bounds)})"
    if generator == "string":
        alphabet = literal(cast(str, source["alphabet"]))
        return f"h2l_generate_string({out}, {source['length']}, {alphabet})"
    if generator == "uuid":
        return f"h2l_generate_uuid({out})"
    if generator == "utc_datetime":
        return f"h2l_generate_datetime({out}, {source['offset_seconds']})"
    bounds = [str(date.fromisoformat(cast(str, source[k])).toordinal())
              for k in ("minimum", "maximum")]
    return f"h2l_generate_date({out}, {', '.join(bounds)})"
