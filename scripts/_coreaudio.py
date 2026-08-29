"""Minimal CoreAudio bindings: list devices with UIDs, create a Multi-Output."""
import ctypes, ctypes.util
from ctypes import c_void_p, c_uint32, c_int32, byref, POINTER, Structure

ca = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

kCFStringEncodingUTF8 = 0x08000100
cf.CFStringCreateWithCString.restype = c_void_p
cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
cf.CFStringGetLength.restype = ctypes.c_long
cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, c_uint32]

def cfstr(s):
    return cf.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)

def from_cfstr(ref):
    if not ref: return None
    p = cf.CFStringGetCStringPtr(c_void_p(ref), kCFStringEncodingUTF8)
    if p: return p.decode()
    n = cf.CFStringGetLength(c_void_p(ref)) * 4 + 1
    buf = ctypes.create_string_buffer(n)
    cf.CFStringGetCString(c_void_p(ref), buf, n, kCFStringEncodingUTF8)
    return buf.value.decode()

class AOPA(Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]

def fourcc(s): return int.from_bytes(s.encode(), "big")

kAudioObjectSystemObject = 1
kAudioHardwarePropertyDevices = fourcc("dev#")
kAudioDevicePropertyDeviceUID = fourcc("uid ")
kAudioObjectPropertyName = fourcc("lnam")
kAudioObjectPropertyScopeGlobal = fourcc("glob")
kAudioObjectPropertyElementMain = 0
kAudioHardwarePropertyDefaultOutputDevice = fourcc("dOut")

ca.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, POINTER(AOPA), c_uint32, c_void_p, POINTER(c_uint32)]
ca.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(AOPA), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
ca.AudioObjectSetPropertyData.argtypes = [c_uint32, POINTER(AOPA), c_uint32, c_void_p, c_uint32, c_void_p]

def devices():
    a = AOPA(kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    size = c_uint32()
    ca.AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, byref(a), 0, None, byref(size))
    n = size.value // 4
    arr = (c_uint32 * n)()
    ca.AudioObjectGetPropertyData(kAudioObjectSystemObject, byref(a), 0, None, byref(size), arr)
    return list(arr)

def dev_str(dev_id, selector):
    a = AOPA(selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    ref = c_void_p()
    size = c_uint32(ctypes.sizeof(c_void_p))
    r = ca.AudioObjectGetPropertyData(dev_id, byref(a), 0, None, byref(size), byref(ref))
    if r != 0: return None
    return from_cfstr(ref.value)

def default_output():
    a = AOPA(kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    d = c_uint32(); size = c_uint32(4)
    ca.AudioObjectGetPropertyData(kAudioObjectSystemObject, byref(a), 0, None, byref(size), byref(d))
    return d.value

def set_default_output(dev_id):
    a = AOPA(kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    d = c_uint32(dev_id)
    return ca.AudioObjectSetPropertyData(kAudioObjectSystemObject, byref(a), 0, None, 4, byref(d))


# --- creating a Multi-Output (stacked aggregate) device ---
import plistlib
cf.CFDataCreate.restype = c_void_p
cf.CFDataCreate.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long]
cf.CFPropertyListCreateWithData.restype = c_void_p
cf.CFPropertyListCreateWithData.argtypes = [c_void_p, c_void_p, c_uint32, c_void_p, c_void_p]
ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]

def make_multi_output(name, uid, sub_uids, master_uid, drift_uids=()):
    """Create a Multi-Output Device. 'stacked'=1 is what makes it multi-output
    rather than a plain aggregate input device."""
    desc = {
        "name": name,
        "uid": uid,
        "stacked": 1,
        "private": 0,
        "master": master_uid,
        "subdevices": [
            {"uid": u, **({"drift": 1} if u in drift_uids else {})}
            for u in sub_uids
        ],
    }
    data = plistlib.dumps(desc, fmt=plistlib.FMT_XML)
    cfdata = cf.CFDataCreate(None, data, len(data))
    cfdict = cf.CFPropertyListCreateWithData(None, c_void_p(cfdata), 0, None, None)
    if not cfdict:
        raise RuntimeError("could not build the device description")
    out = c_uint32()
    r = ca.AudioHardwareCreateAggregateDevice(c_void_p(cfdict), byref(out))
    if r != 0:
        raise RuntimeError(f"AudioHardwareCreateAggregateDevice failed: {r}")
    return out.value
