"""pytest plugin: collect failing test ids (incl. failed subtests and errors) and test
files that fail to collect. Works with xdist (reports reach the controller)."""

import json
import os

_failed: set = set()
_bad: set = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid.split("[")[0] if "::" in report.nodeid else report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _bad.add(report.nodeid.split("::")[0])


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):  # xdist worker: controller writes
        return
    with open(os.environ["FAILS_OUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "bad_files": sorted(_bad)}, f)
