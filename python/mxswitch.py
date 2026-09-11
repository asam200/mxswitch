#!/usr/bin/env python3
"""
mxswitch.py - switch a Logitech Easy-Switch device (MX Master 3S, MX Keys, ...)
to another channel via HID++ 2.0 feature 0x1814 (ChangeHost).

On Windows this needs nothing but a Python 3 interpreter: the HID plumbing goes
straight through hid.dll and setupapi.dll via ctypes. No admin rights - the
vendor-defined collection is not the exclusively-claimed mouse collection.

On macOS/Linux it falls back to hidapi (pip install hidapi), since there is no
equivalent always-present C API to lean on.

    python mxswitch.py --info        # show channels and which one is active
    python mxswitch.py 2             # switch to channel 2
    python mxswitch.py --list        # dump candidate HID interfaces

Exit codes: 0 ok, 1 device not found, 2 bad usage, 3 reset unavailable.
"""

import argparse
import sys
import time

LOGITECH_VID = 0x046D
SW_ID = 0x0A                  # software id, any value 1..15
ROOT_FEATURE = 0x00
FEAT_CHANGE_HOST = 0x1814
FEAT_HOSTS_INFO = 0x1815
FEAT_DEVICE_INFO = 0x0003
FEAT_DEVICE_NAME = 0x0005

SHORT, LONG = 0x10, 0x11      # HID++ report ids
FRAME_LEN = {SHORT: 7, LONG: 20}

# 0xFF for a directly-connected device (Bluetooth/USB), 1..6 via a receiver.
DEVICE_INDICES = (0xFF, 1, 2, 3, 4, 5, 6)


def device_rank(usage_page, usage, name):
    """Lower = try first. Mice before keyboards; vendor pages before others."""
    name = (name or "").lower()
    mouse = ((usage_page == 0x0001 and usage == 0x0002)
             or any(w in name for w in ("master", "anywhere", "mouse", "ergo")))
    kbd = ((usage_page == 0x0001 and usage == 0x0006)
           or any(w in name for w in ("keys", "keyboard")))
    tier = 2 if (kbd and not mouse) else (0 if mouse else 1)
    non_vendor = 0 if usage_page >= 0xFF00 else 1
    return tier * 10 + non_vendor


# ---------------------------------------------------------------------------
# Windows backend: ctypes over the Win32 HID API. No third-party packages.
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid_dll = ctypes.WinDLL("hid", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 0x01, 0x02
    OPEN_EXISTING, FILE_FLAG_OVERLAPPED = 3, 0x40000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_IO_PENDING, WAIT_OBJECT_0 = 997, 0
    HIDP_STATUS_SUCCESS = 0x00110000

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                    ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

    class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", ctypes.c_wchar * 512)]

    class HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                    ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                    ("InputReportByteLength", wintypes.USHORT),
                    ("OutputReportByteLength", wintypes.USHORT),
                    ("FeatureReportByteLength", wintypes.USHORT),
                    ("Reserved", wintypes.USHORT * 17),
                    ("NumberLinkCollectionNodes", wintypes.USHORT),
                    ("NumberInputButtonCaps", wintypes.USHORT),
                    ("NumberInputValueCaps", wintypes.USHORT),
                    ("NumberInputDataIndices", wintypes.USHORT),
                    ("NumberOutputButtonCaps", wintypes.USHORT),
                    ("NumberOutputValueCaps", wintypes.USHORT),
                    ("NumberOutputDataIndices", wintypes.USHORT),
                    ("NumberFeatureButtonCaps", wintypes.USHORT),
                    ("NumberFeatureValueCaps", wintypes.USHORT),
                    ("NumberFeatureDataIndices", wintypes.USHORT)]

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    # Setting restype matters: the default c_int silently truncates 64-bit handles.
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                              wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    hid_dll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]

    def _query(path):
        """Open briefly, read VID/usage/report lengths, close. None if unusable."""
        h = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE:
            return None
        try:
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(attrs)
            if not hid_dll.HidD_GetAttributes(h, ctypes.byref(attrs)):
                return None
            if attrs.VendorID != LOGITECH_VID:
                return None

            pp = ctypes.c_void_p()
            if not hid_dll.HidD_GetPreparsedData(h, ctypes.byref(pp)):
                return None
            try:
                caps = HIDP_CAPS()
                if hid_dll.HidP_GetCaps(pp, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                    return None
            finally:
                hid_dll.HidD_FreePreparsedData(pp)

            if caps.UsagePage < 0xFF00:
                return None                # not a vendor-defined collection

            name = ctypes.create_unicode_buffer(128)
            hid_dll.HidD_GetProductString(h, name, ctypes.sizeof(name))
            return {"path": path, "usage_page": caps.UsagePage, "usage": caps.Usage,
                    "product_id": attrs.ProductID, "product_string": name.value,
                    "in_len": caps.InputReportByteLength,
                    "out_len": caps.OutputReportByteLength}
        finally:
            kernel32.CloseHandle(h)

    def enumerate_interfaces():
        guid = GUID()
        hid_dll.HidD_GetHidGuid(ctypes.byref(guid))
        hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                             DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if hdev == INVALID_HANDLE_VALUE:
            return
        found = []
        try:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            index = 0
            while setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid),
                                                       index, ctypes.byref(iface)):
                index += 1
                detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
                # Documented fixed value, NOT sizeof(): 8 on x64, 6 on x86.
                detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if setupapi.SetupDiGetDeviceInterfaceDetailW(
                        hdev, ctypes.byref(iface), ctypes.byref(detail),
                        ctypes.sizeof(detail), None, None):
                    info = _query(detail.DevicePath)
                    if info:
                        found.append(info)
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(hdev)
        found.sort(key=lambda i: device_rank(i["usage_page"], i["usage"],
                                             i["product_string"]))
        yield from found

    class Device:
        """Overlapped I/O, because a blocking ReadFile on a HID device that has
        nothing to say will simply never return."""

        def __init__(self, info):
            self.in_len, self.out_len = info["in_len"], info["out_len"]
            self.h = kernel32.CreateFileW(
                info["path"], GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED, None)
            if self.h == INVALID_HANDLE_VALUE:
                raise OSError(ctypes.get_last_error(), "CreateFileW failed")
            self.event = kernel32.CreateEventW(None, True, False, None)

        def _wait(self, ov, timeout_ms):
            transferred = wintypes.DWORD()
            if kernel32.WaitForSingleObject(self.event, timeout_ms) != WAIT_OBJECT_0:
                kernel32.CancelIo(self.h)
                return None
            if not kernel32.GetOverlappedResult(self.h, ctypes.byref(ov),
                                                ctypes.byref(transferred), False):
                return None
            return transferred.value

        def write(self, data):
            buf = ctypes.create_string_buffer(
                bytes(data).ljust(self.out_len, b"\x00"), self.out_len)
            ov = OVERLAPPED()
            ov.hEvent = self.event
            kernel32.ResetEvent(self.event)
            if not kernel32.WriteFile(self.h, buf, self.out_len, None, ctypes.byref(ov)):
                if ctypes.get_last_error() != ERROR_IO_PENDING:
                    raise OSError(ctypes.get_last_error(), "WriteFile failed")
                if self._wait(ov, 1000) is None:
                    raise OSError("write timed out")

        def read(self, timeout_ms=100):
            buf = ctypes.create_string_buffer(self.in_len)
            ov = OVERLAPPED()
            ov.hEvent = self.event
            kernel32.ResetEvent(self.event)
            if not kernel32.ReadFile(self.h, buf, self.in_len, None, ctypes.byref(ov)):
                if ctypes.get_last_error() != ERROR_IO_PENDING:
                    return b""
            n = self._wait(ov, timeout_ms)
            return buf.raw[:n] if n else b""

        def close(self):
            kernel32.CloseHandle(self.event)
            kernel32.CloseHandle(self.h)


# ---------------------------------------------------------------------------
# Everything else: hidapi.
# ---------------------------------------------------------------------------

else:
    try:
        import hid
    except ImportError:
        sys.exit("On this platform mxswitch needs hidapi:  pip install hidapi")

    def enumerate_interfaces():
        # Prefer mice over keyboards when both are present. Prefer vendor-defined
        # collections; also yield others — over Bluetooth on macOS, HID++ often
        # lives on the mouse collection alone.
        seen = set()
        found = []
        for info in hid.enumerate(LOGITECH_VID, 0):
            if info["path"] in seen:
                continue
            seen.add(info["path"])
            found.append({"path": info["path"], "usage_page": info["usage_page"],
                          "usage": info["usage"], "product_id": info["product_id"],
                          "product_string": info["product_string"],
                          "in_len": FRAME_LEN[LONG], "out_len": 0})
        found.sort(key=lambda i: device_rank(i["usage_page"], i["usage"],
                                             i["product_string"]))
        yield from found

    class Device:
        def __init__(self, info):
            self.out_len = 0                 # hidapi sizes the frame itself
            self.dev = hid.device()
            self.dev.open_path(info["path"])
            self.dev.set_nonblocking(0)

        def write(self, data):
            self.dev.write(bytes(data))

        def read(self, timeout_ms=100):
            return bytes(self.dev.read(FRAME_LEN[LONG], timeout_ms=timeout_ms))

        def close(self):
            self.dev.close()


# ---------------------------------------------------------------------------
# HID++ protocol
# ---------------------------------------------------------------------------

def request(dev, report_id, dev_idx, feature_idx, function, params=b"", timeout_ms=500):
    """Send one HID++ call and wait for its matching reply. None on timeout/error."""
    frame = bytes([report_id, dev_idx, feature_idx, (function << 4) | SW_ID]) + params
    dev.write(frame.ljust(FRAME_LEN[report_id], b"\x00"))

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        reply = dev.read(timeout_ms=100)
        if len(reply) < 5 or reply[1] != dev_idx:
            continue
        if reply[2] in (0xFF, 0x8F):
            return None                      # HID++ 2.0 / 1.0 error reply
        if reply[2] != feature_idx or (reply[3] & 0x0F) != SW_ID:
            continue                         # unsolicited notification
        return reply
    return None


def get_feature(dev, dev_idx, report_id, feature_id):
    """Resolve a HID++ feature ID through ROOT at runtime."""
    params = bytes([feature_id >> 8, feature_id & 0xFF, 0])
    reply = request(dev, report_id, dev_idx, ROOT_FEATURE, 0, params)
    if not reply or len(reply) < 7 or reply[4] == 0:
        return None
    return {"index": reply[4], "type": reply[5], "version": reply[6]}


def find_device():
    """Return (dev, dev_idx, report_id, changehost_feature_idx, info) or None."""
    probe = bytes([FEAT_CHANGE_HOST >> 8, FEAT_CHANGE_HOST & 0xFF, 0x00])
    for info in enumerate_interfaces():
        try:
            dev = Device(info)
        except OSError:
            continue
        # A collection only carries frames it has room for.
        report_ids = [r for r in (LONG, SHORT)
                      if not info["out_len"] or FRAME_LEN[r] <= info["out_len"]]
        for dev_idx in DEVICE_INDICES:
            for report_id in report_ids:
                try:
                    reply = request(dev, report_id, dev_idx, ROOT_FEATURE, 0,
                                    probe, timeout_ms=250)
                except OSError:
                    continue
                # reply[4] is the feature index; 0 means "not supported"
                if reply and reply[4] != 0x00:
                    return dev, dev_idx, report_id, reply[4], info
        dev.close()
    return None


def get_host_info(dev, dev_idx, report_id, feat_idx):
    """Read ChangeHost fn 0: host count, current host, and capability flags."""
    reply = request(dev, report_id, dev_idx, feat_idx, 0)
    if not reply or len(reply) < 7:
        return None
    host_count, current_host, capabilities = reply[4:7]
    if host_count == 0 or current_host >= host_count:
        return None
    return {
        "host_count": host_count,
        "current_host": current_host,
        "capabilities": capabilities,
    }


def get_cookies(dev, dev_idx, report_id, feat_idx, host_count):
    """Read ChangeHost fn 2: one opaque persistent cookie byte per host."""
    reply = request(dev, report_id, dev_idx, feat_idx, 2)
    if not reply or len(reply) < 4 + host_count:
        return None
    return list(reply[4:4 + host_count])


def get_device_identity(dev, dev_idx, report_id, feat_idx):
    """Read Device Information fn 0 and decode its advertised transport IDs."""
    reply = request(dev, report_id, dev_idx, feat_idx, 0)
    if not reply or len(reply) < 17:
        return None
    transport_bits = reply[10]
    model_id = reply[11:17]
    offset = 0
    transport_ids = {}
    for name, flag in (("btid", 0x01), ("btleid", 0x02),
                       ("wpid", 0x04), ("usbid", 0x08)):
        if transport_bits & flag:
            if offset + 2 > len(model_id):
                return None
            transport_ids[name] = model_id[offset:offset + 2].hex().upper()
            offset += 2
    return {
        "entity_count": reply[4],
        "unit_id": reply[5:9].hex().upper(),
        "transport_ids": transport_ids,
    }


def get_device_name(dev, dev_idx, report_id, feat_idx):
    """Read Device Name fn 0/1 without assuming the model from the USB receiver."""
    reply = request(dev, report_id, dev_idx, feat_idx, 0)
    if not reply or len(reply) < 5:
        return None
    length = reply[4]
    name = bytearray()
    while len(name) < length:
        reply = request(dev, report_id, dev_idx, feat_idx, 1, bytes([len(name)]))
        if not reply or len(reply) <= 4:
            return None
        fragment = reply[4:4 + min(16, length - len(name))]
        if not fragment:
            return None
        name.extend(fragment)
    try:
        return bytes(name).decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_hosts_info(dev, dev_idx, report_id, feat_idx):
    """Read HostsInfo fn 0 capability flags and host count."""
    reply = request(dev, report_id, dev_idx, feat_idx, 0)
    if not reply or len(reply) < 8 or reply[6] == 0:
        return None
    return {
        "capabilities": reply[4],
        "descriptor_capabilities": reply[5],
        "host_count": reply[6],
        "current_host": reply[7],
    }


def get_host_slot_info(dev, dev_idx, report_id, feat_idx, host):
    """Read HostsInfo fn 1 for an explicit zero-based slot."""
    reply = request(dev, report_id, dev_idx, feat_idx, 1, bytes([host]))
    if not reply or len(reply) < 10 or reply[4] != host:
        return None
    return {
        "host": reply[4],
        "status": reply[5],
        "bus_type": reply[6],
        "descriptor_pages": reply[7],
        "name_length": reply[8],
        "name_max_length": reply[9],
    }


def assess_reset_all(dev, dev_idx, report_id):
    """Read advertised reset capability without issuing a mutation command."""
    hosts_feature = get_feature(dev, dev_idx, report_id, FEAT_HOSTS_INFO)
    if not hosts_feature:
        return {
            "available": False,
            "delete_advertised": False,
            "reason": "HostsInfo (0x1815) is not available on this device.",
        }
    hosts = get_hosts_info(dev, dev_idx, report_id, hosts_feature["index"])
    if not hosts:
        return {
            "available": False,
            "delete_advertised": False,
            "reason": "HostsInfo capability data could not be read safely.",
        }
    advertised = bool(hosts["capabilities"] & 0x08)
    if not advertised:
        reason = (
            "This device does not advertise HostsInfo DELETE_HOST capability "
            f"(capabilities=0x{hosts['capabilities']:02X})."
        )
    else:
        reason = (
            "DELETE_HOST is advertised, but no verified public wire command "
            "for deleting a bonding record has been found."
        )
    return {
        "available": False,
        "delete_advertised": advertised,
        "reason": reason,
    }


def format_slot_lines(info, cookies, slots=None):
    """Format slots without treating an opaque cookie as pairing evidence."""
    bus_names = {0: "Undefined", 1: "eQuad", 2: "USB", 3: "BT",
                 4: "BLE", 5: "BLE Pro"}
    lines = []
    for host in range(info["host_count"]):
        current = "yes" if host == info["current_host"] else "no "
        cookie = f"0x{cookies[host]:02X}" if cookies is not None else "unknown"
        slot = slots[host] if slots and host < len(slots) else None
        if slot and slot["status"] in (0, 1):
            paired = "yes" if slot["status"] else "no"
        elif slot:
            paired = f"unknown(raw=0x{slot['status']:02X})"
        else:
            paired = "unknown"
        bus_name = (bus_names.get(slot["bus_type"],
                                  f"raw=0x{slot['bus_type']:02X}")
                    if slot else None)
        bus = f" bus={bus_name}" if bus_name else ""
        lines.append(
            f"Slot {host + 1}: current={current}  cookie={cookie}  paired={paired}{bus}"
        )
    return lines


def set_host(dev, dev_idx, report_id, feat_idx, host_0based):
    """setCurrentHost. Never replies - the link is gone by then."""
    frame = bytes([report_id, dev_idx, feat_idx, (1 << 4) | SW_ID, host_0based])
    dev.write(frame.ljust(FRAME_LEN[report_id], b"\x00"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="target channel (1-3), or reset")
    ap.add_argument("--info", action="store_true", help="show host info and exit")
    ap.add_argument("--list", action="store_true", help="dump HID interfaces and exit")
    ap.add_argument("--all", action="store_true", help="with reset, target all slots")
    ap.add_argument("--experimental", action="store_true",
                    help="required for any operation intended to modify records")
    args = ap.parse_args()

    if args.list:
        for i in enumerate_interfaces():
            print(f"{i['usage_page']:#06x}:{i['usage']:#06x}  pid={i['product_id']:#06x}"
                  f"  out={i['out_len']}  {i['product_string']}")
        return 0

    resetting = args.target == "reset"
    if not args.info and args.target is None:
        ap.error("give a channel number, or --info")
    if resetting:
        if not args.all:
            ap.error("reset currently requires --all")
        if not args.experimental:
            ap.error("reset requires --experimental")
        channel = None
    elif args.target is not None:
        try:
            channel = int(args.target)
        except ValueError:
            ap.error("target must be channel 1, 2 or 3, or reset")
        if not 1 <= channel <= 3:
            ap.error("channel must be 1, 2 or 3")
    else:
        channel = None

    found = find_device()
    if not found:
        print("No Logitech device supporting ChangeHost found.", file=sys.stderr)
        print("Click the mouse once to wake it, then retry. See also --list.",
              file=sys.stderr)
        return 1
    dev, dev_idx, report_id, feat_idx, info = found

    if resetting:
        assessment = assess_reset_all(dev, dev_idx, report_id)
        print("Reset was not performed.", file=sys.stderr)
        print(assessment["reason"], file=sys.stderr)
        print("No pairing, bonding, cookie, NVM, or DFU write was sent.",
              file=sys.stderr)
        return 3

    if args.info:
        device_info_feature = get_feature(dev, dev_idx, report_id, FEAT_DEVICE_INFO)
        name_feature = get_feature(dev, dev_idx, report_id, FEAT_DEVICE_NAME)
        identity = (get_device_identity(dev, dev_idx, report_id,
                                        device_info_feature["index"])
                    if device_info_feature else None)
        device_name = (get_device_name(dev, dev_idx, report_id, name_feature["index"])
                       if name_feature else None)
        print(f"device     : {device_name or info['product_string']}")
        print(f"transport  : {'direct (BT/USB)' if dev_idx == 0xFF else 'receiver'}"
              f"  index={dev_idx:#04x}  report={report_id:#04x}")
        if identity:
            ids = " ".join(f"{key.upper()}={value}"
                           for key, value in identity["transport_ids"].items())
            print(f"identity   : unit={identity['unit_id']} {ids}")
            values = set(identity["transport_ids"].values())
            print(f"MX Master 4 B042: {'yes' if 'B042' in values else 'no'}")
        print(f"ChangeHost : feature index {feat_idx:#04x}")
        host = get_host_info(dev, dev_idx, report_id, feat_idx)
        if host:
            print(f"channels   : {host['host_count']}, currently on {host['current_host'] + 1}")
            print(f"capability : enhanced-host-switch="
                  f"{'yes' if host['capabilities'] & 0x01 else 'no'} "
                  f"raw=0x{host['capabilities']:02X}")
            cookies = get_cookies(dev, dev_idx, report_id, feat_idx,
                                  host["host_count"])
            slots = None
            hosts_feature = get_feature(dev, dev_idx, report_id, FEAT_HOSTS_INFO)
            if hosts_feature:
                hosts = get_hosts_info(dev, dev_idx, report_id,
                                       hosts_feature["index"])
                if hosts:
                    print(f"HostsInfo  : feature index 0x{hosts_feature['index']:02x} "
                          f"version={hosts_feature['version']} capabilities=0x{hosts['capabilities']:02X}")
                    print(f"delete-host advertised="
                          f"{'yes' if hosts['capabilities'] & 0x08 else 'no'}")
                    if hosts["host_count"] == host["host_count"]:
                        slots = [get_host_slot_info(dev, dev_idx, report_id,
                                                    hosts_feature["index"], slot)
                                 for slot in range(host["host_count"])]
            for line in format_slot_lines(host, cookies, slots):
                print(line)
            if slots is None or any(slot is None for slot in slots):
                print("Pairing state is unknown where HostsInfo returned no valid status.")
            print("ChangeHost cookies are metadata, not bonding records.")
        return 0

    print(f"Switching to channel {channel} ...")
    set_host(dev, dev_idx, report_id, feat_idx, channel - 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
