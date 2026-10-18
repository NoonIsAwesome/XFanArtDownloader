"""Bounded transport for one job's independent, reusable extractor process."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time


class WorkerFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WorkerTransport:
    """Serialize source requests; an interrupted source invalidates its worker."""

    def __init__(self, command, stop_event, reader, job_factory, *, queue_size, timeout):
        self.command = command
        self.stop_event, self.reader_fn, self.job_factory = stop_event, reader, job_factory
        self.queue_size, self.timeout = queue_size, timeout
        self.process = self.guard = self.reader = self.output = None
        self.reader_stop = None
        self.request_id = 0
        self.busy = False

    def read(self):
        deadline = time.monotonic() + self.timeout
        while not self.stop_event.is_set():
            try:
                kind, value = self.output.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise WorkerFailure("timeout")
                continue
            if kind != "line":
                raise WorkerFailure("incomplete" if kind == "eof" else "protocol")
            return value
        raise WorkerFailure("cancelled")

    def begin(self, url, conversation):
        # 每輪工作只共享一個程序；同一時間只允許一個來源，不能交錯討論串回應。
        # Reuse one process per job and serialize sources so thread responses cannot interleave.
        if self.busy:
            raise WorkerFailure("protocol")
        if self.process is None:
            environment = os.environ.copy()
            environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYINSTALLER_RESET_ENVIRONMENT="1")
            self.output = queue.Queue(maxsize=self.queue_size)
            self.reader_stop = threading.Event()
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.guard = self.job_factory(self.process)
            self.reader = threading.Thread(target=self.reader_fn,
                args=(self.process.stdout, self.output, self.reader_stop), daemon=True,
                name="gallery-worker-reader")
            self.reader.start()
            try:
                ready = json.loads(self.read().decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise WorkerFailure("protocol") from None
            if ready != {"type": "ready", "protocol": 1}:
                raise WorkerFailure("protocol")
        if self.process.poll() is not None:
            raise WorkerFailure("exit")
        self.busy = True
        self.request_id += 1
        payload = {"request_id": self.request_id, "url": url, "conversation": conversation}
        self.process.stdin.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        return self.request_id

    def finish(self):
        self.busy = False

    def close(self):
        # 完整完成來源才可重用；停止、提早離開與錯誤都回收程序及有界讀取佇列。
        # Only complete sources may reuse the worker; cancellation, early exit and errors reclaim it.
        if self.reader_stop is not None:
            self.reader_stop.set()
        if self.guard is not None:
            self.guard.close()
        process = self.process
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait(timeout=2)
            if self.reader is not None:
                self.reader.join(timeout=2)
            for stream in (process.stdout, process.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        # 已退出的子程序可能讓 stdin 最後一次 flush 失敗；程序仍已確實回收。
                        # A dead peer can reject the final stdin flush after its process was reclaimed.
                        pass
            if self.reader is not None:
                self.reader.join(timeout=0.2)
        self.process = self.guard = self.reader = self.output = None
        self.reader_stop = None
        self.busy = False
