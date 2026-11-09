import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "windows_single_instance.py"


def _load_module():
    assert MODULE_PATH.exists(), "single-instance module has not been implemented"
    spec = importlib.util.spec_from_file_location("windows_single_instance", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeKernel32:
    def __init__(self, last_error):
        self.last_error = last_error
        self.closed = []

    def CreateMutexW(self, _attrs, _owner, _name):
        return 1234

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_first_process_keeps_mutex_and_starts_gui():
    module = _load_module()
    kernel = FakeKernel32(last_error=0)
    restored = []

    primary = module.claim_or_restore(
        kernel32=kernel,
        get_last_error=lambda: kernel.last_error,
        restore_fn=lambda: restored.append(True),
    )

    assert primary is True
    assert module._mutex_handle == 1234
    assert kernel.closed == []
    assert restored == []


def test_second_process_restores_existing_window_and_exits():
    module = _load_module()
    kernel = FakeKernel32(last_error=module.ERROR_ALREADY_EXISTS)
    restored = []

    primary = module.claim_or_restore(
        kernel32=kernel,
        get_last_error=lambda: kernel.last_error,
        restore_fn=lambda: restored.append(True),
    )

    assert primary is False
    assert kernel.closed == [1234]
    assert restored == [True]


def test_gui_checks_single_instance_before_loading_backend():
    source = (ROOT / "tools" / "manage_datasources_gui.py").read_text(
        encoding="utf-8"
    )

    check_pos = source.find("claim_or_restore()")
    backend_pos = source.find("backend, mismatch = get_backend(args.backend)")

    assert check_pos >= 0
    assert backend_pos >= 0
    assert check_pos < backend_pos


def test_main_toolbar_has_no_redundant_close_button():
    source = (ROOT / "tools" / "manage_datasources_gui.py").read_text(
        encoding="utf-8"
    )

    assert '("关闭", self.destroy)' not in source
