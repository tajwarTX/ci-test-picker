"""pytest plugin (mapper only): run one test (MAP_TEST) and record the classes it uses."""

import os

import class_usage

TEST = os.environ.get("MAP_TEST", "")


def pytest_configure(config):
    class_usage.install()
    # spawned worker processes record too (see child_cov.py)
    os.environ["MAP_PARENT_PID"] = str(os.getpid())


def pytest_unconfigure(config):
    import sys

    sys.setprofile(None)
    class_usage.dump(TEST)
