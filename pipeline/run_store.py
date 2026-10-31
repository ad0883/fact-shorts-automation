"""Atomic run checkpoints, integrity-aware caching, and process locking."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def create_run_dir(root: Path, label: str = "short") -> Path:
    # The random suffix prevents repeated titles or simultaneous runs colliding.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{timestamp}-{label[:50]}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


@contextmanager
def run_lock(directory: Path):
    """An OS lock is released automatically on interruption or process death."""
    lock = (directory / ".run.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Another process is using {directory}") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Another process is using {directory}") from exc
        yield
    finally:
        lock.close()


class RunStore:
    def __init__(self, directory: Path, options: dict | None = None):
        self.directory = directory.resolve()
        self.path = directory / "run.json"
        if self.path.exists():
            self.data = read_json(self.path)
            if not isinstance(self.data, dict) or self.data.get("schema_version") != 1:
                raise ValueError("Unsupported run.json format; start a new run using --script fact.json")
        else:
            self.data = {"schema_version": 1, "created_at": utc_now(), "stages": {}, "options": options or {}}

    def save(self, **changes):
        self.data.update(changes, updated_at=utc_now())
        write_json(self.path, self.data)

    def stage(self, name: str, inputs: dict, action) -> bool:
        """Run action unless its inputs and every output still match. Return cached."""
        signature = fingerprint(inputs)
        previous = self.data["stages"].get(name, {})
        if previous.get("status") == "complete" and previous.get("signature") == signature:
            artifacts = previous.get("artifacts", {})
            valid = bool(artifacts)
            for relative, expected in artifacts.items():
                path = (self.directory / relative).resolve()
                if not path.is_relative_to(self.directory) or not path.is_file() or file_hash(path) != expected:
                    valid = False
                    break
            if valid:
                print(f"  {name}: using saved result", flush=True)
                return True
        print(f"  {name}: running…", flush=True)
        record = {"status": "running", "signature": signature, "started_at": utc_now()}
        self.data["stages"][name] = record
        self.save(status="running", current_stage=name, error=None)
        started = time.monotonic()
        try:
            outputs = action()
            artifacts = {}
            for output in outputs:
                output = Path(output).resolve()
                if not output.is_relative_to(self.directory):
                    raise ValueError(f"Stage {name} returned an artifact outside its run")
                if not output.is_file() or output.stat().st_size == 0:
                    raise ValueError(f"Stage {name} did not produce {output.name}")
                artifacts[str(output.relative_to(self.directory))] = file_hash(output)
            record.update(status="complete", artifacts=artifacts, elapsed_seconds=round(time.monotonic() - started, 3))
            self.save()
        except BaseException as exc:
            record.update(status="failed", elapsed_seconds=round(time.monotonic() - started, 3))
            self.save(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
            raise
        return False
