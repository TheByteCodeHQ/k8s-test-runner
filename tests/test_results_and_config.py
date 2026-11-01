import base64

import pytest

from k8s_test_runner.config import ConfigError, load_settings
from k8s_test_runner.results import BEGIN, END, extract_junit, parse_junit, strip_junit
from k8s_test_runner.selector import SelectorError, parse_selector

from .conftest import make_suite

XML = """<testsuites><testsuite>
<testcase classname="a" name="ok" time="0.5"/>
<testcase classname="a" name="bad" time="1.0"><failure message="boom">trace</failure></testcase>
<testcase classname="a" name="err" time="0.1"><error message="kaput">trace</error></testcase>
<testcase name="skip" time="0"><skipped message="later"/></testcase>
</testsuite></testsuites>"""


def logged(xml=XML, before="some output\n", after=""):
    payload = base64.b64encode(xml.encode()).decode()
    return f"{before}{BEGIN}\n{payload}\n{END}\n{after}"


def test_extract_and_parse_results():
    xml = extract_junit(logged())
    results = parse_junit(xml, "r1")
    assert (results.total, results.passed, results.failed, results.error, results.skipped) == (4, 1, 1, 1, 1)
    assert results.results[1].test_id == "a::bad" and results.results[1].message == "boom"
    assert results.duration_seconds == 1.6


def test_last_marker_block_wins_and_garbage_is_ignored():
    assert "<testcase" in extract_junit(logged(before=logged(xml="<a/>")))
    assert extract_junit("no markers here") is None
    assert extract_junit(f"{BEGIN}\nnot base64!!\n{END}") is None
    assert extract_junit(f"{BEGIN}\n{base64.b64encode(b'x').decode()}") is None  # missing end marker


def test_log_view_hides_the_encoded_payload():
    shown = strip_junit(logged(after="done\n"))
    assert "some output" in shown and "done" in shown and "[JUnit results captured]" in shown
    assert "PHRlc3RzdWl0ZXM" not in shown and BEGIN not in shown


@pytest.mark.parametrize("selector, expected", [
    (None, []), ("-k login", ["-k", "login"]), ('-k "a and not b"', ["-k", "a and not b"]),
    ("tests/test_x.py::test_y[1-2]", ["tests/test_x.py::test_y[1-2]"]),
])
def test_valid_selectors(selector, expected):
    assert parse_selector(selector, make_suite()) == expected


@pytest.mark.parametrize("selector", [
    "--junitxml=/etc/x", "-p evil", "--rootdir=/", "../x", "/etc/passwd", "a; b", "a && b", "$(id)", "`id`",
    "a|b", "C:\\x", "C:x", "-k " + "x" * 500, " ".join(["a"] * 25), '-k "open',
])
def test_blocked_selectors(selector):
    with pytest.raises(SelectorError):
        parse_selector(selector, make_suite())


def test_closed_suite_refuses_selectors():
    with pytest.raises(SelectorError):
        parse_selector("-k a", make_suite(allow_selector=False))


def write(tmp_path, text):
    path = tmp_path / "suites.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path):
    settings = load_settings(write(tmp_path, """
executor: local
max_active_runs: 2
suites:
  api-tests:
    image: example/api-tests:1
    command: ["pytest", "--junitxml={junit}"]
    secret_env: { TOKEN: { secret: s, key: k } }
    resources: { limits: { memory: 1Gi } }
"""))
    suite = settings.suites["api-tests"]
    assert settings.executor == "local" and settings.max_active_runs == 2
    assert suite.secret_env["TOKEN"].secret == "s" and suite.resources.memory_limit == "1Gi"


@pytest.mark.parametrize("body, message", [
    ("suites: {}", "at least one suite"),
    ("suites: { Bad_Name: { image: x, command: [a, '{junit}'] } }", "name must be"),
    ("suites: { a: { command: [a, '{junit}'] } }", "'image' is required"),
    ("suites: { a: { image: x, command: 'pytest {junit}' } }", "list of strings"),
    ("suites: { a: { image: x, command: [pytest] } }", "JUnit"),
    ("suites: { a: { image: x, command: [a, '{junit}'], timeout_seconds: 1 } }", "timeout_seconds"),
    ("suites: { a: { image: x, command: [a, '{junit}'], resources: { limits: { cpu: lots } } } }", "quantity"),
    ("suites: { a: { image: x, command: [a, '{junit}'], secret_env: { T: { secret: s } } } }", "secret_env"),
    ("executor: docker\nsuites: { a: { image: x, command: [a, '{junit}'] } }", "executor must be"),
])
def test_invalid_configs(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, body))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")
