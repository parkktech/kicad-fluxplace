"""Refuse to write a board that KiCad has open.

KiCad 6-10 (LOCKFILE, common/lockfile.cpp) creates `~<name>.<ext>.lck` beside the file, e.g. `~board.kicad_pcb.lck`, holding JSON
{"hostname": H, "username": U}; it is removed on a clean close, so a stale one after a crash also reads as held (fail closed:
--force-open overrides).  Self-block check: pcbnew.LoadBoard/SaveBoard and kicad-cli never create a lock (only the GUI frames do;
verified by loading + saving a board and listing the directory), so no fluxplace-made lock needs excluding."""
import json
import os


def lock_path(path):
    d, n = os.path.split(os.path.abspath(path))
    return os.path.join(d, "~" + n + ".lck")


def held(path):
    """Lock info dict {lock, user, host} if the board is locked, else None. An unreadable/odd lock content still counts as held."""
    lp = lock_path(path)
    if not os.path.exists(lp):
        return None
    user = host = "?"
    try:
        d = json.load(open(lp))
        user, host = str(d.get("username", "?")), str(d.get("hostname", "?"))
    except Exception:
        pass
    return {"lock": os.path.basename(lp), "user": user, "host": host}


class BoardOpenError(RuntimeError):
    pass


def guard(path, force=False):
    """Raise BoardOpenError when `path` is open in KiCad, unless force."""
    if force or not path:
        return
    h = held(path)
    if h:
        raise BoardOpenError(f"board is open in KiCad (lock {h['lock']}, user {h['user']} on host {h['host']}): close it or pass --force-open")
