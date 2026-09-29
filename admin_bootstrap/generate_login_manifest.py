"""Refresh diagnostic pin, then source manifest; never access installed state."""
import hashlib
from pathlib import Path
import re
import runpy


def refresh(root):
    diagnose = root / 'admin_bootstrap/login_diagnose.py'
    api = runpy.run_path(str(diagnose))
    bootstrap = root / 'admin_bootstrap/bootstrap.py'
    original = api['source_read'](root, 'admin_bootstrap/bootstrap.py')
    digest = hashlib.sha256(api['source_read'](root, 'admin_bootstrap/login_diagnose.py')).hexdigest()
    updated, count = re.subn(rb"(?m)^DIAGNOSE_SOURCE_SHA256 = '[0-9a-f]{64}'$",
                             ("DIAGNOSE_SOURCE_SHA256 = '" + digest + "'").encode(), original)
    if count != 1:
        raise ValueError('diagnostic-pin-format')
    if updated != original:
        bootstrap.write_bytes(updated)
    manifest = root / 'admin_bootstrap/login_manifest.json'
    generated = api['generate_manifest'](root)
    if manifest.read_bytes() != generated:
        manifest.write_bytes(generated)
    api['verify_source'](root)


if __name__ == '__main__':
    refresh(Path(__file__).resolve().parents[1])
    print('source-manifest=ok')
