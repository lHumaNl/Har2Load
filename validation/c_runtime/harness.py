"""Build only fixed emitted code + synthetic inputs, never traffic or authored code."""

from pathlib import Path
from subprocess import CalledProcessError

from har2load.loadrunner import LoadRunnerGenerator
from har2load.loadrunner_runtime import helpers
from validation.support import run_isolated

HERE = Path(__file__).parent.resolve()
FLAGS = ["-std=c99", "-g", "-O1", "-fsanitize=address,undefined",
         "-fno-omit-frame-pointer", "-fno-sanitize-recover=all"]
PRELUDE = ('#include "stubs.h"\n#define malloc test_malloc\n'
           '#define calloc test_calloc\n#define free test_free\n')
POSTLUDE = '\n#undef malloc\n#undef calloc\n#undef free\n'


def isolated(command, directory):
    try:
        return run_isolated([str(item) for item in command], directory)
    except CalledProcessError as error:
        raise AssertionError(f"Synthetic C command failed: {command}\n"
                             f"stdout:\n{error.stdout}\nstderr:\n{error.stderr}") from error


def compile_source(directory, source):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "driver.c").write_text(source, encoding="ascii")
    version = isolated(["/usr/bin/clang", "--version"], directory)
    (directory / "compiler.txt").write_text(version.stdout)
    command = ["/usr/bin/clang", *FLAGS, "-I", HERE, "-I", directory,
               directory / "driver.c", "-o", directory / "driver"]
    result = isolated(command, directory)
    (directory / "compile.txt").write_text(result.stdout + result.stderr)
    return directory / "driver"


def helper_binary(directory):
    source = PRELUDE + "\n".join(helpers()) + POSTLUDE
    return compile_source(directory, source + '\n#include "helper_driver.c"\n')


def lifecycle_binary(directory, plan, checks):
    build = LoadRunnerGenerator().generate(plan)
    assert build.status == "generated", build.diagnostics
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in build.files.items():
        (directory / Path(name).name).write_bytes(data)
    source = PRELUDE + '#include "Action.c"\n#include "vuser_init.c"\n#include "vuser_end.c"\n'
    return compile_source(directory, source + POSTLUDE + checks)


def run_cases(binary, rows, name):
    file = binary.parent / (name + ".cases")
    file.write_text("".join("\t".join(map(str, row)) + "\n" for row in rows), encoding="ascii")
    result = isolated([binary, file], binary.parent)
    (binary.parent / (name + ".out")).write_text(result.stdout)
    assert not result.stderr, result.stderr
    outputs = [line.split("\t") for line in result.stdout.splitlines()]
    assert len(outputs) == len(rows)
    assert all(row[-1] == "0" for row in outputs), "Unbalanced emitted allocations"
    return outputs


def hx(value):
    return (value.encode("utf-8") if isinstance(value, str) else value).hex()


def c_literal(value):
    """Independent fixture encoder: every UTF-8 byte is a fixed-width octal escape."""
    return '"' + "".join(f"\\{byte:03o}" for byte in value.encode("utf-8")) + '"'


def json_case(body, path=(), occurrence=1, empty=True, expected_type=0, fail_at=-1):
    tokens = ",".join("k" + hx(p) if isinstance(p, str) else "i" + str(p) for p in path)
    return ("J", hx(body), tokens, occurrence, int(empty), expected_type, fail_at)


def result(row):
    assert row[0] == "R"
    return (bool(int(row[1])), bool(int(row[2])), int(row[3]), int(row[4]),
            bool(int(row[5])), bytes.fromhex(row[6]).decode("utf-8"))
