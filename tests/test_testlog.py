from harvest import testlog

RA_LOG = """\
============================= test session starts ==============================
tests/test_a.py ..F                                                      [100%]
=========================== short test summary info ============================
PASSED tests/test_a.py::test_ok
PASSED tests/test_a.py::test_param[a b]
FAILED tests/test_a.py::test_bug - AssertionError: assert 1 == 2
ERROR tests/test_b.py::test_setup - ImportError: nope
SKIPPED [1] tests/test_a.py:10: needs network
XFAIL tests/test_a.py::test_known - reason
\x1b[32mPASSED\x1b[0m tests/test_a.py::test_colour
"""


def test_parse_rA_summary():
    s = testlog.parse_pytest_log(RA_LOG)
    assert s["tests/test_a.py::test_ok"] == "PASSED"
    assert s["tests/test_a.py::test_bug"] == "FAILED"
    assert s["tests/test_b.py::test_setup"] == "ERROR"
    assert s["tests/test_a.py::test_known"] == "XFAIL"
    assert s["tests/test_a.py::test_colour"] == "PASSED"
    # ids containing spaces are reachable both in full and in the split()[1] form datasets record
    assert s["tests/test_a.py::test_param[a b]"] == "PASSED"
    assert s["tests/test_a.py::test_param[a"] == "PASSED"
    assert not any(k.startswith("[") for k in s)


def test_parse_verbose_lines():
    log = "tests/t.py::test_x PASSED                [ 50%]\ntests/t.py::TestC::test_y FAILED\n"
    s = testlog.parse_pytest_log(log)
    assert s == {"tests/t.py::test_x": "PASSED", "tests/t.py::TestC::test_y": "FAILED"}


def test_judge_contract():
    f2p, p2p = ["t::bug"], ["t::ok"]
    good = testlog.judge(f2p, p2p, {"t::bug": "FAILED", "t::ok": "PASSED"}, {"t::bug": "PASSED", "t::ok": "PASSED"})
    assert good["verified"] and good["reason"] is None
    # a missing F2P result before the fix (e.g. collection error) counts as failing
    assert testlog.judge(f2p, p2p, {}, {"t::bug": "PASSED", "t::ok": "PASSED"})["verified"]
    assert testlog.judge(f2p, p2p, {"t::bug": "PASSED"}, {"t::bug": "PASSED", "t::ok": "PASSED"})["reason"] == "f2p_passes_without_fix"
    assert testlog.judge(f2p, p2p, {}, {"t::bug": "FAILED", "t::ok": "PASSED"})["reason"] == "f2p_fails_with_fix"
    assert testlog.judge(f2p, p2p, {}, {"t::bug": "PASSED", "t::ok": "SKIPPED"})["reason"] == "p2p_fails_with_fix"
    assert testlog.judge(f2p, p2p, {}, {})["reason"] == "no_tests_collected"
