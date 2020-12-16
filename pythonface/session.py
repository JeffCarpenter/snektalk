import asyncio
import builtins
import inspect
import json
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import count

from hrepr import H, Tag, hrepr

from .registry import UNAVAILABLE, callback_registry

_c = count(1)

_current_session = ContextVar("current_session", default=None)
_current_evalid = ContextVar("current_evalid", default=None)


def current_session():
    return _current_session.get()


@contextmanager
def new_evalid():
    token = _current_evalid.set(next(_c))
    try:
        yield
    finally:
        _current_evalid.reset(token)


class Evaluator:
    def __init__(self, session, glb):
        self.session = session
        self.glb = glb

    def push(self):
        self.session.push_evaluator(self)

    def pop(self):
        self.session.pop_evaluator()

    def activate(self):
        self.session.queue(
            command="set_mode",
            html=H.span["pf-input-mode-python"](">>>"),
        )

    def deactivate(self):
        pass

    def eval(self, expr):
        try:
            return eval(expr, self.session.glb)
        except SyntaxError:
            exec(expr, self.session.glb)
            return None

    def run(self, thing):
        if isinstance(thing, str):
            self.session.schedule(
                self.session.direct_send(
                    command="echo",
                    value=thing,
                )
            )

        try:
            if isinstance(thing, str):
                result = self.eval(thing)
                typ = "statement" if result is None else "expression"
            else:
                result = thing()
                typ = "expression"
        except Exception as e:
            result = e
            typ = "exception"

        self.session.blt["_"] = result

        self.session.schedule(
            self.session.send_result(result, type=typ)
        )


class Session:
    def __init__(self, glb, socket):
        self.glb = glb
        self.blt = vars(builtins)
        self.idmap = {}
        self.varcount = count(1)
        self.socket = socket
        self.sent_resources = set()
        self.evaluators = []
        self.loop = asyncio.get_running_loop()

    @property
    def current_evaluator(self):
        return self.evaluators[-1] if self.evaluators else None

    def push_evaluator(self, ev):
        if self.current_evaluator:
            self.current_evaluator.deactivate()
        self.evaluators.append(ev)
        ev.activate()

    def pop_evaluator(self):
        assert self.current_evaluator
        self.current_evaluator.deactivate()
        self.evaluators.pop()
        assert self.current_evaluator
        self.current_evaluator.activate()

    @contextmanager
    def set_context(self):
        token = _current_session.set(self)
        try:
            yield
        finally:
            _current_session.reset(token)

    async def direct_send(self, **command):
        """Send a command to the client."""
        await self.socket.send(json.dumps(command))

    async def send(self, **command):
        """Send a command to the client, plus any resources.

        Any field that is a Tag and contains resources will send
        resource commands to the client to load these resources.
        A resource is only sent once, the first time it is needed.
        """
        resources = []
        for k, v in command.items():
            if isinstance(v, Tag):
                resources.extend(v.collect_resources())
                command[k] = str(v)

        for resource in resources:
            if resource not in self.sent_resources:
                await self.direct_send(
                    command="resource",
                    value=str(resource),
                )
                self.sent_resources.add(resource)

        evalid = _current_evalid.get()
        if evalid is not None:
            command["evalid"] = evalid

        await self.direct_send(**command)

    def schedule(self, fn):
        self.loop.create_task(fn)

    def queue(self, **command):
        """Queue a command to the client, plus any resources.

        This queues the command using the session's asyncio loop.
        """
        self.schedule(self.send(**command))

    def newvar(self):
        """Create a new variable."""
        return f"_{next(self.varcount)}"

    def getvar(self, obj):
        """Get the variable name corresponding to the object.

        If the object is not already associated to a variable, one
        will be created and set in the global scope.
        """
        ido = id(obj)
        if ido in self.idmap:
            varname = self.idmap[ido]
        else:
            varname = self.newvar()
            self.idmap[ido] = varname
        self.blt[varname] = obj
        return varname

    def represent(self, typ, result):
        if isinstance(result, Tag):
            return typ, result

        try:
            html = hrepr(result)
        except Exception as exc:
            try:
                html = hrepr(exc)
            except Exception:
                html = H.pre(
                    traceback.format_exception(
                        builtins.type(exc), exc, exc.__traceback__
                    )
                )
                typ = "hrepr_exception"
        return typ, html

    async def send_result(self, result, *, type):
        type, html = self.represent(type, result)
        await self.send(command="result", value=html, type=type)

    def run(self, thing):
        with self.set_context():
            with new_evalid():
                self.current_evaluator.run(thing)

    async def recv(self, **command):
        cmd = command.pop("command", "none")
        meth = getattr(self, f"command_{cmd}", None)
        await meth(**command)

    async def command_submit(self, *, expr):
        self.run(expr)

    async def command_callback(self, *, id, response_id, arguments):
        try:
            cb = callback_registry.resolve(int(id))
        except KeyError:
            self.queue(
                command="status",
                type="error",
                value="value is unavailable; it might have been garbage-collected",
            )
            return

        try:
            with self.set_context():
                if inspect.isawaitable(cb):
                    result = await cb(*arguments)
                else:
                    result = cb(*arguments)

            await self.send(
                command="response",
                value=result,
                response_id=response_id,
            )

        except Exception as exc:
            await self.send(
                command="response",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc.args[0]) if exc.args else None,
                },
                response_id=response_id,
            )
