"""Per-process memory facts for ops (s3 r2): RSS, peak RSS, pid, process start. Read from /proc (Linux); elsewhere
the memory fields are None. Every worker heartbeat carries them, and each bounded create records a sample, so a
process that dies after a create can be told apart from one that grew."""
import os
import socket

from core.util import now_iso

PROCESS_STARTED_AT = now_iso()  # imported at server start (core.jobs imports this module)


def _status_kb(field: str) -> int | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def proc_mem() -> dict:
    rss, peak = _status_kb("VmRSS"), _status_kb("VmHWM")
    return {"pid": os.getpid(), "host": socket.gethostname(), "process_started_at": PROCESS_STARTED_AT,
            "rss_mb": round(rss / 1024, 1) if rss is not None else None,
            "peak_rss_mb": round(peak / 1024, 1) if peak is not None else None}
