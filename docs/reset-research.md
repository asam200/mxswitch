# Easy-Switch host-record reset research

Status: reset writes are disabled. This note records what the current code and
public implementations establish, and what is still missing.

## Confirmed read protocol

All feature indices are resolved at runtime with ROOT `0x0000`, function 0.
No feature index below is hardcoded.

| Feature | Confirmed calls used here | Result |
|---|---|---|
| `0x0003 Device Information` | function 0 | unit ID and transport model IDs, including `B042` detection |
| `0x0005 Device Name` | functions 0 and 1 | device name length and chunks |
| `0x1814 ChangeHost` | function 0 | host count, current host, capability byte |
| `0x1814 ChangeHost` | function 2 | one opaque cookie byte per host |
| `0x1815 HostsInfo` | function 0 | capability flags, descriptor capability, host count, current host |
| `0x1815 HostsInfo` | function 1 | slot status, bus type, descriptor/name metadata |

`0x1814` function 1 remains the existing `setCurrentHost` operation. Function 3
is documented by OpenLogi and logiops as setting a cookie. Neither function
deletes a pairing or bonding record.

## Captured device evidence

The Windows test machine currently exposes an MX Master 3S through a receiver,
not an MX Master 4. Its Device Information response identifies `BTLEID=B034`.
Read-only feature discovery returned:

```text
0x0003 -> index 0x02, version 4
0x0005 -> index 0x03, version 0
0x1814 -> index 0x0A, version 1
0x1815 -> index 0x0B, version 2
0x1D4B -> index 0x04, version 0
```

Relevant replies, with trailing zero padding omitted where it carries no data:

```text
DeviceInfo fn0: 11 02 02 0A 03 81 85 8A AB 00 02 B0 34 00 00 00 00 01 01 00
HostsInfo fn0: 11 02 0B 0A 13 08 03 00
HostsInfo fn1 slot 0: 11 02 0B 1A 00 01 05 01 0F 18
HostsInfo fn1 slot 1: 11 02 0B 1A 01 00 00 00 00 18
HostsInfo fn1 slot 2: 11 02 0B 1A 02 01 04 01 0F 18
```

The three ChangeHost cookies were all `0x00`, while HostsInfo reported slots 0
and 2 as paired. This directly disproves the assumption that a zero cookie means
an empty slot. The tool therefore reports pairing only from HostsInfo status 0
(empty) or 1 (paired); other or missing values are `unknown`.

The captured HostsInfo capability byte is `0x13`: GET_NAME, SET_NAME, and
SET_OS_VERSION. Bit `0x08` (`DELETE_HOST`) is clear. This device does not
advertise host deletion.

## Public implementation review

- [Logitech cpg-docs HID++ 2.0 overview](https://github.com/Logitech/cpg-docs/blob/master/hidpp20/README.rst)
  establishes runtime feature discovery through ROOT. The public repository does
  not contain an `0x1815` delete request definition.
- [Solaar ChangeHost](https://github.com/pwr-Solaar/Solaar/blob/master/lib/logitech_receiver/settings_templates.py)
  uses ChangeHost function 0 to read and function 1 to switch.
  [Solaar HostsInfo](https://github.com/pwr-Solaar/Solaar/blob/master/lib/logitech_receiver/hidpp20.py)
  reads host information and names but has no host-delete operation.
- [OpenLogi ChangeHost](https://openlogi.org/hidpp/features/x1814-change-host)
  defines get/set host and get/set cookie. It describes the cookie as persistent
  software metadata, not a bonding record.
- [OpenLogi HostsInfo](https://openlogi.org/hidpp/features/x1815-hosts-info)
  names a `DELETE_HOST` capability bit. Its
  [Rust implementation](https://github.com/AprilNEA/OpenLogi/blob/master/crates/openlogi-hidpp/src/feature/hosts_info.rs)
  implements feature info, host info, and descriptor reads, but does not provide
  a delete function number or packet encoding.
- [logiops ChangeHost](https://git.tannercollin.com/tanner/logiops/src/commit/a0687c8f18e312824ac04043c410b637ce89e371/src/logid/backend/hidpp20/features/ChangeHost.cpp)
  confirms functions 0 through 3 and contains no erase operation.
- [logitune HID protocol notes](https://github.com/mmaher88/logitune/wiki/HID---Protocol)
  cover ChangeHost reads. Their cookie-based pairing inference conflicts with the
  captured B034 evidence above and is not used.
- [logifetch reverse-engineering notes](https://github.com/procdeveloper/logifetch/blob/main/reverse/README.md)
  identify the MX Master 4 direct-BLE endpoint as `046D:B042` with usage
  `FF43:0202`, but do not define a host-record deletion command.

## Reset decision

There is currently evidence that some firmware can advertise a host-delete
capability, but no verified public wire command that maps it to a function ID,
parameters, and response. Sending a guessed call would violate the project's
safety rules and could target a different operation at the dynamically assigned
feature index.

`reset --all --experimental` therefore performs only ROOT and HostsInfo reads,
prints why reset is unavailable, and exits with code 3. Implementation of the
write path requires either an authoritative `0x1815` protocol document or a
controlled capture of Logitech software issuing the delete operation on an MX
Master 4 B042, followed by a reproducible read-back showing the slot became
empty. DeviceReset, receiver unpair commands, cookie clearing, DFU, firmware
updates, undocumented NVM writes, and fuzzing are outside this path.
