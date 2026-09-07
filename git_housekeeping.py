"""Fixed-repository Git housekeeping with no raw output in responses."""
from pathlib import PurePosixPath
import re
import subprocess
import threading

PROJECT_DIR = "/home/ermis/projects/epoptia-bridge"
_LOCK = threading.Lock()


def _git(*args, capture=False):
    # Do not inherit Git directory, index, config or identity overrides.
    return subprocess.run(
        ["/usr/bin/git", "--git-dir", PROJECT_DIR + "/.git",
         "--work-tree", PROJECT_DIR, "-c", "core.hooksPath=/dev/null",
         "-c", "commit.gpgSign=false", *args],
        cwd=PROJECT_DIR, env={"HOME": "/home/ermis", "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True, timeout=30, check=False, shell=False,
    )


def _sensitive(path):
    parts = PurePosixPath(path).parts
    return any(p.lower().startswith((".env", "credentials", "private_key"))
               or p.lower() in {".ssh", "id_rsa", "id_ed25519", "id_ecdsa"}
               or p.lower().endswith((".pem", ".key", ".p12", ".pfx"))
               for p in parts)


def commit(message: str) -> dict:
    """Stage all non-ignored changes and commit using configured Git identity.

    Refuse known sensitive paths; ignored files are never force-added. Unknown
    post-operation state is represented by null. Hooks and signing are disabled
    for this bounded operation, without writing Git configuration.
    """
    result = dict(ok=False, committed=False, commit_hash=None, clean=None)
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        return dict(result, error="A non-empty commit message is required")
    with _LOCK:
        try:
            for field in ("user.name", "user.email"):
                identity = _git("config", "--get", field, capture=True)
                if identity.returncode != 0 or not identity.stdout.strip():
                    return dict(result, error="Git identity is missing")
            paths = _git("ls-files", "--cached", "--others", "--exclude-standard",
                         "-z", capture=True)
            if paths.returncode != 0:
                return dict(result, error="Git preflight failed")
            if any(_sensitive(p) for p in paths.stdout.split("\x00") if p):
                return dict(result, error="Sensitive paths prevent committing")
            if _git("add", "--all", "--", ".").returncode != 0:
                result["error"] = "Git staging failed"
            else:
                staged = _git("diff", "--cached", "--quiet", "--exit-code")
                if staged.returncode == 0:
                    result["ok"] = True
                elif staged.returncode == 1:
                    created = _git("commit", "--no-verify", "--message", message)
                    if created.returncode == 0:
                        result.update(ok=True, committed=True)
                    else:
                        result["error"] = "Git commit failed"
                else:
                    result["error"] = "Git staged-state check failed"
            head = _git("rev-parse", "--verify", "HEAD", capture=True)
            if head.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head.stdout.strip()):
                result["commit_hash"] = head.stdout.strip()[:12]
            status = _git("status", "--porcelain=v1", "--untracked-files=normal", capture=True)
            if status.returncode == 0:
                result["clean"] = not bool(status.stdout)
        except (OSError, UnicodeError, subprocess.SubprocessError, ValueError):
            result.update(ok=False, error="Git operation unavailable or timed out")
        return result
