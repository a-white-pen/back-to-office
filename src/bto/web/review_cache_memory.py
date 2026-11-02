"""Account for retained Review objects and bound its optional caches."""

import sys
import threading
from collections import OrderedDict

MIB = 1024 * 1024


def object_bytes(value, seen=None):
    """Count retained Python objects once, including their containers and keys.

    Native buffers and allocator slack need separate process headroom."""
    seen = set() if seen is None else seen
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(object_bytes(k, seen) + object_bytes(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list, set, frozenset)):
        size += sum(object_bytes(v, seen) for v in value)
    elif hasattr(value, "__dict__"):
        size += object_bytes(vars(value), seen)
    return size


class ObjectIds:
    """Track object identities in compact bitmap pages without retaining the objects.

    The page key preserves unaligned identity bits so membership stays exact."""

    def __init__(self):
        self.pages = {}

    def __contains__(self, identity):
        page = self.pages.get(((identity >> 15) << 3) | (identity & 7))
        offset = (identity >> 3) & 4095
        return page is not None and bool(page[offset >> 3] & (1 << (offset & 7)))

    def add(self, identity):
        key = ((identity >> 15) << 3) | (identity & 7)
        page = self.pages.get(key)
        if page is None:
            page = self.pages[key] = bytearray(512)
        offset = (identity >> 3) & 4095
        page[offset >> 3] |= 1 << (offset & 7)


class ByteLRU:
    """A thread-safe LRU bounded by retained Python bytes and entry count."""

    def __init__(self, max_bytes, max_entries):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._items = OrderedDict()
        self._lock = threading.Lock()
        self.bytes = 0

    def get(self, key, default=None):
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return default
            self._items.move_to_end(key)
            return entry[0]

    def put(self, key, value):
        # Include entry/key/container bookkeeping, not just the returned body.
        charge = object_bytes((key, value)) + 128
        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self.bytes -= old[1]
            if charge > self.max_bytes:
                return False
            while self._items and (
                self.bytes + charge > self.max_bytes or len(self._items) >= self.max_entries
            ):
                self.bytes -= self._items.popitem(last=False)[1][1]
            self._items[key] = value, charge
            self.bytes += charge
        return True

    def clear(self):
        with self._lock:
            self._items.clear()
            self.bytes = 0

    def __len__(self):
        with self._lock:
            return len(self._items)

    def __contains__(self, key):
        with self._lock:
            return key in self._items


def memory_usage():
    """Current Linux process bytes and host-available bytes, when exposed."""
    try:
        with open("/proc/self/status") as handle:
            process = dict(line.split(":", 1) for line in handle if ":" in line)
        used = sum(int(process.get(key, "0 kB").split()[0]) for key in ("VmRSS", "VmSwap")) * 1024
        with open("/proc/meminfo") as handle:
            host = dict(line.split(":", 1) for line in handle if ":" in line)
        available = int(host.get("MemAvailable", "0 kB").split()[0]) * 1024
        return used, available
    except (OSError, ValueError):
        # Local macOS has different accounting; its cache object limits still apply.
        return None


def memory_pressure(max_bytes=350 * MIB):
    """Include swapped process memory and require host headroom."""
    usage = memory_usage()
    return bool(usage and (usage[0] > max_bytes or 0 < usage[1] < 128 * MIB))
