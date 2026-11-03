"""The ordinary tests/ suite never discovers or enables native execution."""

import os

import pytest

from validation.support import prove_network_denial


@pytest.fixture(scope="session", autouse=True)
def native_opt_in(tmp_path_factory):
    if os.environ.get("HAR2LOAD_NATIVE_VALIDATION") != "1":
        pytest.skip("Separate native harness: set HAR2LOAD_NATIVE_VALIDATION=1 explicitly")
    prove_network_denial(tmp_path_factory.mktemp("network-probe"))
