"""Build progress and persistent subprocess logs, shared by both build contracts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

from . import RigError
from .common import eprint


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}m {seconds:02d}s'


class BuildProgress:
    def __init__(self, labels, *, root=None, verbose=False, interval=15):
        self.labels = labels
        self.root = root
        self.verbose = verbose
        self.interval = interval
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.active = {}
        self.completed = self.failed = 0

    def __enter__(self):
        parent = Path(self.root) / 'var' / 'build' if self.root is not None else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
        self.directory = Path(tempfile.mkdtemp(prefix=stamp if parent else 'rig-build-logs-', dir=parent))
        self.started = time.monotonic()
        self.write(f'rig build: {len(self.labels)} steps (builds and mirrors)')
        self.write(f'rig build: logs: {self.directory}')
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        self.thread.join()
        with self.lock:
            state = 'stopped' if exc_type is KeyboardInterrupt else 'failed' if exc_type or self.failed else 'finished'
            self.write(f'rig build: {state} in {duration(time.monotonic() - self.started)}; {self._counts()}')

    def write(self, message):
        eprint(message, flush=True)

    def _counts(self):
        remaining = len(self.labels) - self.completed - self.failed
        failed = f', {self.failed} failed' if self.failed else ''
        return f'{self.completed}/{len(self.labels)} complete{failed}, {remaining} remaining'

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                for index, (started, phase) in self.active.items():
                    self.write(f'[{index + 1}/{len(self.labels)}] {phase} {self.labels[index]} '
                               f'({duration(time.monotonic() - started)} elapsed); {self._counts()}')

    @contextmanager
    def step(self, index):
        label = self.labels[index]
        slug = re.sub(r'[^a-zA-Z0-9_.-]+', '-', label).strip('-')[:100] or 'build'
        log = self.directory / f'{index + 1:03d}-{slug}.log'
        log.touch()
        started = time.monotonic()
        with self.lock:
            self.active[index] = (started, 'RUNNING')
            self.write(f'[{index + 1}/{len(self.labels)}] START {label}; {self._counts()}')
        try:
            yield BuildStep(self, index, log)
        except BaseException as exc:
            with log.open('a') as handle:
                handle.write(f'\nrig: {exc}\n')
            with self.lock:
                self.active.pop(index)
                self.failed += 1
                self.write(f'[{index + 1}/{len(self.labels)}] FAILED {label} '
                           f'({duration(time.monotonic() - started)}); {self._counts()}')
                if not self.verbose:
                    with log.open('rb') as handle:
                        handle.seek(max(0, log.stat().st_size - 16384))
                        tail = handle.read().decode('utf-8', errors='replace').splitlines()[-40:]
                    self.write('\n'.join(tail))
                self.write(f'Full log: {log}')
            raise
        else:
            with self.lock:
                self.active.pop(index)
                self.completed += 1
                self.write(f'[{index + 1}/{len(self.labels)}] DONE {label} '
                           f'({duration(time.monotonic() - started)}); {self._counts()}')


class BuildStep:
    def __init__(self, progress, index, log):
        self.progress, self.index, self.log = progress, index, log

    def verifying(self):
        with self.progress.lock:
            started, _ = self.progress.active[self.index]
            self.progress.active[self.index] = (started, 'VERIFYING')

    def run(self, command, **kwargs):
        """Stream to disk, optionally teeing live output; never buffer a build in memory."""
        try:
            with self.log.open('a') as log:
                if self.progress.verbose:
                    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, errors='replace', **kwargs) as process:
                        try:
                            for line in process.stdout:
                                log.write(line)
                                log.flush()
                                with self.progress.lock:
                                    self.progress.write(f'[{self.index + 1}] {line.rstrip()}')
                            code = process.wait()
                        except BaseException:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            raise
                else:
                    code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, **kwargs).returncode
        except OSError as exc:
            raise RigError(f'cannot run build command: {exc}') from exc
        if code:
            raise RigError(f'command failed with exit code {code}')
        return subprocess.CompletedProcess(command, code)
