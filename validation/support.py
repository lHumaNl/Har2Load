"""Fail-closed native subprocess isolation for the separately invoked test harness."""

import os
import subprocess
import sys
from pathlib import Path

SANDBOX = Path("/usr/bin/sandbox-exec")
POLICY = "(version 1) (allow default) (deny network*)"


def run_isolated(
    command: list[str], cwd: Path, *, timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Execute trusted synthetic tests, denying all network including loopback.

    No fallback is permitted on unsupported hosts. This is network isolation and
    a minimal environment, not a sandbox for arbitrary captured/third-party code.
    """
    if os.environ.get("HAR2LOAD_NATIVE_VALIDATION") != "1":
        raise RuntimeError("Native validation requires explicit opt-in")
    if sys.platform != "darwin" or not SANDBOX.is_file():
        raise RuntimeError("No supported network-denying sandbox; refusing execution")
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(cwd), "TMPDIR": str(cwd),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
        "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=1",
    }
    return subprocess.run([str(SANDBOX), "-p", POLICY, *command], cwd=cwd,
                          env=environment, text=True, capture_output=True,
                          check=True, timeout=timeout)


def prove_network_denial(cwd: Path) -> None:
    result = run_isolated([sys.executable, "-I", "-S", "-c", """
import errno, socket
s = socket.socket()
try:
    s.connect(('127.0.0.1', 9))
except OSError as error:
    assert error.errno == errno.EPERM, (error.errno, str(error))
else:
    raise AssertionError('Network sandbox did not deny connection')
finally:
    s.close()
print('NETWORK_DENIED')
"""], cwd)
    if result.stdout.strip() != "NETWORK_DENIED":
        raise RuntimeError("Network sandbox probe failed")
