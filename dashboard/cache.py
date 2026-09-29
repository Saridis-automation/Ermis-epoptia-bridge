"""Best-effort local display cache; no upstream responses or diagnostics."""
import json
import logging
import os
from pathlib import Path
import tempfile


class SnapshotCache:
    def __init__(self, directory):
        self.directory = Path(directory) if directory is not None else None

    def load(self, name):
        if self.directory is None:
            return None
        try:
            with (self.directory / f'{name}.json').open() as stream:
                data = json.load(stream)
            return data if data.get('version') == 1 else None
        except (OSError, ValueError, AttributeError):
            return None

    def save(self, name, snapshot):
        if self.directory is None:
            return
        temporary = None
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as stream:
                temporary = stream.name
                json.dump(dict(version=1, snapshot=snapshot), stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f'{name}.json')
        except (OSError, ValueError, TypeError):
            logging.getLogger(__name__).warning('Dashboard cache write unavailable; retaining memory cache')
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
