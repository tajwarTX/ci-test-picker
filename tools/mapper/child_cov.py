"""Started from a .pth file in every Python process of the mapper image.

If this process is a CHILD of a mapped pytest run (e.g. a ProcessPoolExecutor worker
spawned by test_optimizer / test_executor), record its coverage under the parent
test's id, so work done in worker processes is credited to that test.
"""

import os


def start():
    parent = os.environ.get("MAP_PARENT_PID")
    test = os.environ.get("MAP_TEST")
    if not parent or not test or parent == str(os.getpid()):
        return
    import atexit

    if os.environ.get("MAP_MODE") == "opens":
        import opens_plugin

        opens_plugin.install()
        atexit.register(opens_plugin.dump)
        real_exit_o = os._exit

        def _exit_opens(code):
            try:
                opens_plugin.dump()
            finally:
                real_exit_o(code)

        os._exit = _exit_opens
        return

    if os.environ.get("MAP_MODE") == "usage":
        import class_usage

        class_usage.install()
        atexit.register(class_usage.dump, test)
        real_exit = os._exit

        def _exit_usage(code):
            try:
                class_usage.dump(test)
            finally:
                real_exit(code)

        os._exit = _exit_usage
        return

    import coverage

    src = os.environ.get("MAP_SRC", "/src")
    cov = coverage.Coverage(
        data_file=os.path.join(os.environ.get("MAP_OUT", "/out"), ".coverage"),
        data_suffix=True,
        source=[os.path.join(src, "sqlglot"), os.path.join(src, "tests")],
        context=test,
    )
    cov.start()

    def _save():
        cov.stop()
        cov.save()

    atexit.register(_save)
    # Pool workers can leave through os._exit, which skips atexit: save first.
    real_exit = os._exit

    def _exit(code):
        try:
            _save()
        finally:
            real_exit(code)

    os._exit = _exit


try:
    start()
except Exception:
    pass
