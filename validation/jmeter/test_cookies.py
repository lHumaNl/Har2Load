"""Opt-in native initial-cookie fixtures; no HTTP engine, sampling, or captured source."""

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from har2load.cookies import NativeCookie
from har2load.jmeter import JMeterGenerator
from tests.test_jmeter import ARTIFACT, plan
from tests.test_jmeter_cookies import same_name_seeds, seed
from tests.test_jmeter_runtime import plan as runtime_plan
from validation.support import run_isolated


def variants() -> dict[str, tuple[NativeCookie, ...]]:
    cookie = seed()
    return {
        "empty": (), "host_only": (cookie,),
        "domain": (replace(cookie, domain_specified=True, path_specified=True),),
        "insecure": (replace(cookie, secure=False),),
        "empty_value": (replace(cookie, value=""),),
        "safe_ascii": (replace(cookie, value="!#%&'()*+-./:<=>?@[]^_`{|}~"),),
        "token_name": (replace(cookie, name="!#%&'*+-.^_`|~012AZaz"),),
        "root_path": (replace(cookie, path="/"),),
        "explicit_path": (replace(cookie, path_specified=True),),
        "same_name_paths": same_name_seeds(),
    }


def probes(cookies: tuple[NativeCookie, ...]) -> list[dict[str, str | None]]:
    if len(cookies) > 1:
        return same_name_probes(cookies)
    cookie = cookies[0] if cookies else seed()
    host = "[" + cookie.domain + "]" if ":" in cookie.domain else cookie.domain
    header = cookie.name + "=" + cookie.value if cookies else None
    result = [{"url": "https://" + host + path, "header": header} for path in (
        cookie.path, cookie.path.rstrip("/") + "/item", cookie.path + "?q=synthetic")]
    result += [{"url": "http://" + host + cookie.path,
                "header": header if not cookie.secure else None},
               {"url": "https://unrelated.invalid" + cookie.path, "header": None}]
    if ":" not in cookie.domain:
        result.append({"url": "https://sub." + host + cookie.path,
                       "header": header if cookie.domain_specified else None})
    if cookie.path != "/":
        result += [{"url": "https://" + host + path, "header": None}
                   for path in ("/", "/application", "/other")]
    return result


def same_name_probes(cookies: tuple[NativeCookie, ...]) -> list[dict[str, str | None]]:
    root, app = cookies
    root_header = root.name + "=" + root.value
    app_header = app.name + "=" + app.value + "; " + root_header
    origin = "https://" + root.domain
    return [{"url": origin + "/app", "header": app_header},
            {"url": origin + "/", "header": root_header},
            {"url": origin + "/app/item", "header": app_header},
            {"url": origin + "/application", "header": root_header},
            {"url": "http://" + root.domain + "/app", "header": None},
            {"url": "https://sub." + root.domain + "/app", "header": None},
            {"url": "https://unrelated.invalid/app", "header": None}]


def write_cases(directory: Path) -> Path:
    cases = []
    for profile, factory in (("static", plan), ("runtime", runtime_plan)):
        for variant, cookies in variants().items():
            name = profile + "_" + variant
            build = JMeterGenerator().generate(replace(factory(), initial_cookies=cookies))
            assert build.status == "generated", build.diagnostics
            path = directory / (name + ".jmx")
            path.write_bytes(build.files[ARTIFACT])
            cases.append({"name": name, "jmx": str(path), "probes": probes(cookies),
                          "cookies": [asdict(cookie) for cookie in cookies],
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = directory / "cases.json"
    manifest.write_text(json.dumps(cases, indent=2))
    return manifest


def native_command(manifest: Path) -> list[str]:
    home = Path(os.environ["HAR2LOAD_JMETER_HOME"])
    java = Path(os.environ["HAR2LOAD_JAVA8_HOME"]) / "bin/java"
    jars = sorted((home / "lib").glob("*.jar")) + sorted((home / "lib/ext").glob("*.jar"))
    return [str(java), "-Xmx512m", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
            "-cp", os.pathsep.join(map(str, jars)), "groovy.ui.GroovyMain",
            str(Path(__file__).with_name("CookieHarness.groovy")), str(home), str(manifest)]


def execute_harness(manifest: Path, directory: Path) -> dict[str, Any]:
    try:
        result = run_isolated(native_command(manifest), directory, timeout=180)
    except Exception as error:
        (directory / "failure-stdout.txt").write_text(getattr(error, "stdout", "") or "")
        (directory / "failure-stderr.txt").write_text(getattr(error, "stderr", "") or "")
        pytest.fail(f"Cookie harness failed: {getattr(error, 'stdout', '')}\n"
                    f"{getattr(error, 'stderr', '')}\n{error}")
    (directory / "stdout.txt").write_text(result.stdout)
    (directory / "stderr.txt").write_text(result.stderr)
    report: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    (directory / "execution-evidence.json").write_text(json.dumps(report, indent=2))
    return report


def test_native_cookie_manager_lifecycle_and_capability_controls(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    report = execute_harness(manifest, tmp_path)
    for control in ("function_negative_control", "past_expiry_negative_control",
                    "gui_roundtrip_flag_loss_control", "ipv6_host_mismatch_control"):
        assert report[control] == "passed"
    expected = {case["name"]: case["sha256"] for case in json.loads(manifest.read_text())}
    assert {case["name"]: case["jmx_sha256"] for case in report["cases"]} == expected
    assert all(case["status"] == "passed" and case["workers"] == 2 for case in report["cases"])
