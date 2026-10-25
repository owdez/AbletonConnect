"""Command registry and main-thread execution.

Handlers are plain functions `fn(ctx, **args)` registered with @command. They
run on Live's main thread only. A handler returns JSON-ready data, or a
generator for work that spans several ticks (browser search, waiting for a
device to load). A generator yields None to say "check the time budget",
WAIT to say "resume on the next tick", and returns its result.
"""

import contextlib
import inspect
import time

from .protocol import BridgeError, error, ok

WAIT = object()

COMMANDS = {}


class Command:
    def __init__(self, name, fn, undo):
        self.name = name
        self.fn = fn
        self.undo = undo
        self.signature = inspect.signature(fn)
        self.params = [p for p in list(self.signature.parameters)[1:]]


def command(name=None, undo=False):
    """Register a handler. undo=True wraps the call in one Live undo step."""

    def register(fn):
        cmd_name = name or fn.__name__
        COMMANDS[cmd_name] = Command(cmd_name, fn, undo)
        return fn

    return register


class Context:
    """What handlers see. Wraps the ControlSurface so handlers stay testable."""

    def __init__(self, song_getter, application_getter):
        self._song_getter = song_getter
        self._application_getter = application_getter
        self.cache = {}

    @property
    def song(self):
        return self._song_getter()

    @property
    def application(self):
        return self._application_getter()

    @property
    def browser(self):
        return self._application_getter().browser

    @contextlib.contextmanager
    def undo_step(self):
        song = self.song
        begin = getattr(song, "begin_undo_step", None)
        if begin is None:
            yield
            return
        begin()
        try:
            yield
        finally:
            song.end_undo_step()


class _Job:
    def __init__(self, request_id, cmd, gen, reply, timeout):
        self.request_id = request_id
        self.cmd = cmd
        self.gen = gen
        self.reply = reply
        self.deadline = time.monotonic() + timeout


class Dispatcher:
    def __init__(self, ctx, log, job_timeout=60.0):
        self.ctx = ctx
        self.log = log
        self.job_timeout = job_timeout
        self._jobs = []

    @property
    def job_count(self):
        return len(self._jobs)

    def handle(self, request, reply):
        """Run one request. Main thread only. Never raises."""
        request_id = request.get("id")
        cmd = request.get("cmd")
        try:
            command_ = COMMANDS.get(cmd)
            if command_ is None:
                reply(error(request_id, "unknown_command", "Unknown command %r." % cmd))
                return
            args = request.get("args") or {}
            problem = _check_args(command_, args)
            if problem:
                reply(error(request_id, "bad_args", problem, where=cmd))
                return
            if command_.undo:
                with self.ctx.undo_step():
                    result = command_.fn(self.ctx, **args)
            else:
                result = command_.fn(self.ctx, **args)
        except Exception as e:
            reply(self._failure(request_id, cmd, e))
            return
        if inspect.isgenerator(result):
            self._jobs.append(_Job(request_id, cmd, result, reply, self.job_timeout))
            return
        reply(ok(request_id, result))

    def run_jobs(self, deadline):
        """Advance multi-tick jobs until `deadline` (time.monotonic())."""
        for job in list(self._jobs):
            if time.monotonic() >= deadline:
                return
            self._advance(job, deadline)

    def cancel_jobs(self, message):
        for job in self._jobs:
            try:
                job.gen.close()
            except Exception:
                pass
            job.reply(error(job.request_id, "cancelled", message, where=job.cmd))
        self._jobs = []

    def _advance(self, job, deadline):
        try:
            while True:
                if time.monotonic() > job.deadline:
                    job.gen.close()
                    self._finish(job, error(job.request_id, "timeout", "%s ran longer than %.0f s inside Live." % (job.cmd, self.job_timeout), where=job.cmd))
                    return
                step = next(job.gen)
                if step is WAIT or time.monotonic() >= deadline:
                    return
        except StopIteration as stop:
            self._finish(job, ok(job.request_id, stop.value))
        except Exception as e:
            self._finish(job, self._failure(job.request_id, job.cmd, e))

    def _finish(self, job, response):
        if job in self._jobs:
            self._jobs.remove(job)
        job.reply(response)

    def _failure(self, request_id, cmd, exc):
        if isinstance(exc, BridgeError):
            return error(request_id, exc.type, exc.message, where=cmd)
        self.log.exception("%s failed" % cmd)
        kind = "live_error" if isinstance(exc, RuntimeError) else "internal"
        return error(request_id, kind, "%s: %s" % (type(exc).__name__, exc), where=cmd)


def _check_args(command_, args):
    params = command_.signature.parameters
    unknown = sorted(k for k in args if k not in params or k == "ctx")
    if unknown:
        return "Unknown argument(s) %s. %s takes: %s." % (", ".join(unknown), command_.name, ", ".join(command_.params) or "nothing")
    missing = [
        name
        for name in command_.params
        if params[name].default is inspect.Parameter.empty and name not in args
    ]
    if missing:
        return "Missing argument(s) %s for %s." % (", ".join(missing), command_.name)
    return None
