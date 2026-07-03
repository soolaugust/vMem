"""Compatibility shim for memory_os.store.vfs."""

from importlib import import_module as _import_module

_mod = _import_module("memory_os.store.vfs")
globals().update({k: v for k, v in _mod.__dict__.items() if k not in {"__name__", "__loader__", "__package__", "__spec__"}})
