"""Pick the audio output for the alarm via CoreAudio (ctypes, no dependencies).

Why: the system default output is often an external monitor (HDMI/DisplayPort).
When the display sleeps, its speakers go silent and the alarm is inaudible. So
during an alarm we temporarily switch the default output to the built-in
speakers (or a configured device) and switch back afterwards.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import struct

log = logging.getLogger(__name__)


def _fourcc(s: str) -> int:
    return struct.unpack(">I", s.encode())[0]


SYSTEM_OBJECT = 1
SEL_DEVICES = _fourcc("dev#")
SEL_DEFAULT_OUTPUT = _fourcc("dOut")
SEL_NAME = _fourcc("lnam")
SEL_TRANSPORT = _fourcc("tran")
SEL_STREAMS = _fourcc("stm#")
SCOPE_GLOBAL = _fourcc("glob")
SCOPE_OUTPUT = _fourcc("outp")
TRANSPORT_BUILTIN = _fourcc("bltn")
CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


_ca = _cf = None


def _libs():
    global _ca, _cf
    if _ca is None:
        _ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        _ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        _ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _ca, _cf


def _size(obj: int, sel: int, scope: int = SCOPE_GLOBAL) -> int:
    ca, _ = _libs()
    n = ctypes.c_uint32(0)
    err = ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(_Address(sel, scope, 0)), 0, None, ctypes.byref(n))
    return 0 if err else n.value


def _get_u32(obj: int, sel: int) -> int | None:
    ca, _ = _libs()
    v, n = ctypes.c_uint32(0), ctypes.c_uint32(4)
    err = ca.AudioObjectGetPropertyData(obj, ctypes.byref(_Address(sel, SCOPE_GLOBAL, 0)), 0, None,
                                        ctypes.byref(n), ctypes.byref(v))
    return None if err else v.value


def _name(dev: int) -> str:
    ca, cf = _libs()
    ref, n = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    if ca.AudioObjectGetPropertyData(dev, ctypes.byref(_Address(SEL_NAME, SCOPE_GLOBAL, 0)), 0, None,
                                     ctypes.byref(n), ctypes.byref(ref)) or not ref.value:
        return ""
    buf = ctypes.create_string_buffer(512)
    cf.CFStringGetCString(ref, buf, 512, CF_UTF8)
    cf.CFRelease(ref)
    return buf.value.decode("utf-8", "replace")


def output_devices() -> list[dict]:
    """[{id, name, builtin}] for devices that have output streams."""
    try:
        size = _size(SYSTEM_OBJECT, SEL_DEVICES)
        count = size // 4
        arr = (ctypes.c_uint32 * count)()
        n = ctypes.c_uint32(size)
        ca, _ = _libs()
        if ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(_Address(SEL_DEVICES, SCOPE_GLOBAL, 0)), 0,
                                         None, ctypes.byref(n), arr):
            return []
    except OSError as e:
        log.warning("CoreAudio unavailable: %s", e)
        return []
    out = []
    for dev in arr:
        if _size(dev, SEL_STREAMS, SCOPE_OUTPUT) > 0:
            out.append({"id": int(dev), "name": _name(dev), "builtin": _get_u32(dev, SEL_TRANSPORT) == TRANSPORT_BUILTIN})
    return out


def default_output() -> int | None:
    try:
        return _get_u32(SYSTEM_OBJECT, SEL_DEFAULT_OUTPUT)
    except OSError:
        return None


def set_default_output(dev: int) -> bool:
    ca, _ = _libs()
    v = ctypes.c_uint32(dev)
    err = ca.AudioObjectSetPropertyData(SYSTEM_OBJECT, ctypes.byref(_Address(SEL_DEFAULT_OUTPUT, SCOPE_GLOBAL, 0)),
                                        0, None, 4, ctypes.byref(v))
    if err:
        log.warning("could not switch audio output (CoreAudio error %s)", err)
    return not err


def resolve(choice: str) -> dict | None:
    """'builtin' -> built-in speakers, 'default' -> None (leave as is), else name substring."""
    choice = (choice or "builtin").strip()
    if choice.lower() == "default":
        return None
    devs = output_devices()
    if choice.lower() == "builtin":
        return next((d for d in devs if d["builtin"]), None)
    return next((d for d in devs if choice.lower() in d["name"].lower()), None)
