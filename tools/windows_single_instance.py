# -*- coding: utf-8 -*-
"""Windows single-instance guard for the datasource manager GUI."""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\db-auto-router-datasource-manager-v5"
WINDOW_TITLE_PREFIX = "数据源管理 · db-auto-router"

SW_SHOW = 5
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

_mutex_handle = None


def _configure_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _find_window(user32, title_prefix: str):
    found = []
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

    @callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if buffer.value.startswith(title_prefix):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_callback, 0)
    return found[0] if found else None


def restore_existing_window(title_prefix: str = WINDOW_TITLE_PREFIX) -> bool:
    """Restore and focus the existing Tk window, retrying during cold startup."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for _ in range(20):
        hwnd = _find_window(user32, title_prefix)
        if hwnd:
            command = SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW
            user32.ShowWindow(hwnd, command)
            user32.SetWindowPos(
                hwnd,
                HWND_TOP,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            return True
        time.sleep(0.1)
    return False


def claim_or_restore(kernel32=None, get_last_error=None, restore_fn=None) -> bool:
    """Return True for the primary process; restore the primary and return False otherwise."""
    global _mutex_handle
    if sys.platform != "win32" and kernel32 is None:
        return True

    kernel32 = kernel32 or _configure_kernel32()
    get_last_error = get_last_error or ctypes.get_last_error
    restore_fn = restore_fn or restore_existing_window

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        restore_fn()
        return False

    _mutex_handle = handle
    return True
