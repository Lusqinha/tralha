"""Throwaway process that decodes images uploaded by third parties.

A bug in an image decoder must not turn into access to the server. The process starts
with an empty environment and locks itself down in two steps:

1. It caps memory, CPU time, write size and open files, and it may not create processes
   (RLIMIT_NPROC=0).
2. It loads the codecs and, before opening the image, enables Landlock: it cannot run
   programs or open TCP connections, it can read only the input file, and it can write
   only to the output folder created for this job.

Without Landlock in the kernel (Linux < 5.13, or blocked by the container) the worker
runs with the resource limits alone and says so on stderr; the caller logs the warning.
"""

import ctypes
import json
import os
import resource
import sys

from tralha import imaging

EXIT_OK = 0
EXIT_REJECTED = 4
LANDLOCK_UNAVAILABLE = "landlock-unavailable"

RESOURCE_LIMITS = {
    resource.RLIMIT_AS: 1024 * 1024 * 1024,
    resource.RLIMIT_CPU: 20,
    resource.RLIMIT_FSIZE: 64 * 1024 * 1024,
    resource.RLIMIT_NOFILE: 64,
    resource.RLIMIT_CORE: 0,
    resource.RLIMIT_NPROC: 0,
}

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_MAKE_REG = 1 << 8
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_IOCTL_DEV = 1 << 15
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


_libc = ctypes.CDLL(None, use_errno=True)


def apply_resource_limits() -> None:
    for limit, value in RESOURCE_LIMITS.items():
        resource.setrlimit(limit, (value, value))


def _landlock_abi() -> int:
    version = _libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(version, 0)


def _check(result: int) -> int:
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return result


def _allow(ruleset_fd: int, path: str, access: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _PathBeneathAttr(allowed_access=access, parent_fd=fd)
        _check(
            _libc.syscall(
                _SYS_LANDLOCK_ADD_RULE,
                ctypes.c_int(ruleset_fd),
                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                ctypes.byref(rule),
                ctypes.c_uint32(0),
            )
        )
    finally:
        os.close(fd)


def confine_to(read_file: str, write_dir: str) -> bool:
    abi = _landlock_abi()
    if abi < 1:
        return False

    handled_fs = _FS_REFER - 1
    truncate = _FS_TRUNCATE if abi >= 3 else 0
    handled_fs |= (_FS_REFER if abi >= 2 else 0) | truncate
    handled_fs |= _FS_IOCTL_DEV if abi >= 5 else 0
    attr = _RulesetAttr(
        handled_access_fs=handled_fs,
        handled_access_net=(_NET_BIND_TCP | _NET_CONNECT_TCP) if abi >= 4 else 0,
        scoped=(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if abi >= 6 else 0,
    )
    attr_size = 8 if abi < 4 else 16 if abi < 6 else 24

    ruleset_fd = _check(
        _libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.c_size_t(attr_size),
            ctypes.c_uint32(0),
        )
    )
    try:
        _allow(ruleset_fd, read_file, _FS_READ_FILE)
        _allow(ruleset_fd, write_dir, _FS_READ_FILE | _FS_WRITE_FILE | _FS_MAKE_REG | truncate)
        _check(_libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        _check(
            _libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        )
    finally:
        os.close(ruleset_fd)
    return True


def main() -> int:
    apply_resource_limits()
    job = json.loads(sys.argv[1])
    imaging.preload_codecs()
    operation = imaging.OPERATIONS[job.pop("op")]
    if not confine_to(job["src"], os.path.dirname(job["dest"])):
        print(LANDLOCK_UNAVAILABLE, file=sys.stderr)

    try:
        operation(**job)
    except imaging.Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return EXIT_REJECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
