"""Which commit is running. `git archive` fills build_info.txt (export-subst); a git checkout asks git."""
import subprocess
from functools import lru_cache
from pathlib import Path

_INFO = Path(__file__).with_name("build_info.txt")


@lru_cache(maxsize=1)
def build_info() -> dict:
    text = _INFO.read_text().strip() if _INFO.exists() else ""
    if text and not text.startswith("$Format"):
        short, full, at = (text.split() + ["", "", ""])[:3]
        return {"commit": short, "commit_full": full, "committed_at": at, "source": "git-archive"}
    try:
        out = subprocess.run(["git", "-C", str(_INFO.parent), "log", "-1", "--format=%h %H %cI"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        if len(out) == 3:
            return {"commit": out[0], "commit_full": out[1], "committed_at": out[2], "source": "git"}
    except (OSError, subprocess.SubprocessError):
        pass
    return {"commit": "unknown", "commit_full": None, "committed_at": None, "source": "unknown"}
