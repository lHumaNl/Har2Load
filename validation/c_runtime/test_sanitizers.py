"""Negative controls prove sanitizer faults are fatal in the same sandbox."""

from subprocess import CalledProcessError

import pytest

from validation.c_runtime.harness import compile_source
from validation.support import run_isolated


@pytest.mark.parametrize("source,diagnostic", [
    ('#include <stdlib.h>\nint main(int argc,char **argv) {\n'
     ' volatile char *p = malloc(1); (void)argv; p[argc] = 1; free((void *)p); return 0; }',
     "AddressSanitizer: heap-buffer-overflow"),
    ('#include <limits.h>\nint main(void) { volatile int n = INT_MAX; return n + 1; }',
     "runtime error: signed integer overflow"),
])
def test_sanitizer_negative_control_is_fatal(tmp_path, source, diagnostic):
    binary = compile_source(tmp_path, source)
    with pytest.raises(CalledProcessError) as failure:
        run_isolated([str(binary)], tmp_path)
    assert diagnostic in failure.value.stderr
    (tmp_path / "expected-sanitizer-failure.txt").write_text(failure.value.stderr)
