"""ctypes wrapper over the fpkernel C ABI (kernel/src/lib.rs). Board I/O is s-expression text in Rust; no pcbnew, no kicad-cli.

`Kernel(path, **opts)` opens a board, `.call(op, **kw)` returns the reply dict or raises KernelError.
"""
import ctypes
import json
import os

LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fpkernel.so")
_lib = None


class KernelError(RuntimeError):
    pass


def _load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(LIB_PATH)
        lib.fpk_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.fpk_open.restype = ctypes.c_void_p
        lib.fpk_call.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.fpk_call.restype = ctypes.c_void_p
        lib.fpk_raster.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t]
        lib.fpk_raster.restype = ctypes.c_int64
        lib.fpk_close.argtypes = [ctypes.c_void_p]
        lib.fpk_close.restype = None
        lib.fpk_free_str.argtypes = [ctypes.c_void_p]
        lib.fpk_free_str.restype = None
        lib.fpk_last_error.argtypes = []
        lib.fpk_last_error.restype = ctypes.c_char_p
        _lib = lib
    return _lib


def available():
    """True when the built library exists and loads (kernel/build.sh)."""
    if not os.path.isfile(LIB_PATH):
        return False
    try:
        _load()
        return True
    except OSError:
        return False


def _last_error():
    return (_load().fpk_last_error() or b"").decode("utf-8", "replace")


class Kernel:
    def __init__(self, path, **opts):
        lib = _load()
        self._h = lib.fpk_open(os.fspath(path).encode(), json.dumps(opts).encode())
        if not self._h:
            raise KernelError(f"fpk_open failed for {path}: {_last_error()}")

    def call(self, op, **kw):
        lib = _load()
        p = lib.fpk_call(self._h, json.dumps({"op": op, **kw}).encode())
        try:
            reply = json.loads(ctypes.string_at(p).decode("utf-8"))
        finally:
            lib.fpk_free_str(p)
        if not reply.get("ok"):
            raise KernelError(f"{op}: {reply.get('error')}")
        return reply

    def close(self):
        if self._h:
            _load().fpk_close(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
