import multiprocessing
import signal
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Union, Optional, List

from hologram import JsonSchemaMixin, ValidationError
from hologram.helpers import StrEnum

import dbt.exceptions
from dbt.adapters.factory import cleanup_connections
from dbt.logger import GLOBAL_LOGGER as logger, list_handler, LogMessage
from dbt.rpc.error import (
    dbt_error,
    server_error,
    RPCException,
    timeout_error,
)
from dbt.rpc.logger import (
    RemoteCallableResult,
    QueueSubscriber,
    QueueLogHandler,
    QueueErrorMessage,
    QueueResultMessage,
    QueueTimeoutMessage,
)
from dbt.rpc.task import RPCTask
from dbt.utils import env_set_truthy

# we use this in typing only...
from queue import Queue  # noqa


SINGLE_THREADED_HANDLER = env_set_truthy('DBT_SINGLE_THREADED_HANDLER')


class TaskHandlerState(StrEnum):
    NotStarted = 'not started'
    Initializing = 'initializing'
    Running = 'running'
    Success = 'success'
    Error = 'error'

    def __lt__(self, other) -> bool:
        """A logical ordering for TaskHandlerState:

        NotStarted < Initializing < Running < (Success, Error)
        """
        if not isinstance(other, TaskHandlerState):
            raise TypeError('cannot compare to non-TaskHandlerState')
        order = (self.NotStarted, self.Initializing, self.Running)
        smaller = set()
        for value in order:
            smaller.add(value)
            if self == value:
                return other not in smaller

        return False

    def __le__(self, other) -> bool:
        # so that ((Success <= Error) is True)
        return ((self < other) or
                (self == other) or
                (self.finished and other.finished))

    def __gt__(self, other) -> bool:
        if not isinstance(other, TaskHandlerState):
            raise TypeError('cannot compare to non-TaskHandlerState')
        order = (self.NotStarted, self.Initializing, self.Running)
        smaller = set()
        for value in order:
            smaller.add(value)
            if self == value:
                return other in smaller
        return other in smaller

    def __ge__(self, other) -> bool:
        # so that ((Success <= Error) is True)
        return ((self > other) or
                (self == other) or
                (self.finished and other.finished))

    @property
    def finished(self) -> bool:
        return self == self.Error or self == self.Success


def sigterm_handler(signum, frame):
    raise dbt.exceptions.RPCKilledException(signum)


def _task_bootstrap(
    task: RPCTask,
    queue,  # typing: Queue[Tuple[QueueMessageType, Any]]
    params: JsonSchemaMixin,
) -> None:
    """_task_bootstrap runs first inside the child process"""
    signal.signal(signal.SIGTERM, sigterm_handler)
    # the first thing we do in a new process: push logging back over our queue
    handler = QueueLogHandler(queue)
    with handler.applicationbound():
        rpc_exception = None
        result = None
        try:
            result = task.handle_request(params=params)
        except RPCException as exc:
            rpc_exception = exc
        except dbt.exceptions.RPCKilledException as exc:
            # do NOT log anything here, you risk triggering a deadlock on the
            # queue handler we inserted above
            rpc_exception = dbt_error(exc)
        except dbt.exceptions.Exception as exc:
            logger.debug('dbt runtime exception', exc_info=True)
            rpc_exception = dbt_error(exc)
        except Exception as exc:
            logger.debug('uncaught python exception', exc_info=True)
            rpc_exception = server_error(exc)

        # put whatever result we got onto the queue as well.
        if rpc_exception is not None:
            handler.emit_error(rpc_exception.error)
        elif result is not None:
            handler.emit_result(result)
        else:
            error = dbt_error(dbt.exceptions.InternalException(
                'after request handling, neither result nor error is None!'
            ))
            handler.emit_error(error.error)


class RequestTaskHandler(threading.Thread):
    """Handler for the single task triggered by a given jsonrpc request."""
    def __init__(self, manager, task, http_request, json_rpc_request):
        self.manager = manager
        self.task = task
        self.http_request = http_request
        self.json_rpc_request = json_rpc_request
        self.subscriber: Optional[QueueSubscriber] = None
        self.process: Optional[multiprocessing.Process] = None
        self.thread: Optional[threading.Thread] = None
        self.started: Optional[datetime] = None
        self.ended: Optional[datetime] = None
        self.timeout: Optional[float] = None
        self.task_id: uuid.UUID = uuid.uuid4()
        # the are multiple threads potentially operating on these attributes:
        #   - the task manager has the RequestTaskHandler and any requests
        #     might access it via ps/kill, but only for reads
        #   - The actual thread that this represents, which writes its data to
        #     the result and logs. The atomicity of list.append() and item
        #     assignment means we don't need a lock.
        self.result: Optional[JsonSchemaMixin] = None
        self.error: Optional[RPCException] = None
        self.state: TaskHandlerState = TaskHandlerState.NotStarted
        self.logs: List[LogMessage] = []
        super().__init__(
            name='{}-handler-{}'.format(self.task_id, self.method),
            daemon=True,  # if the RPC server goes away, we probably should too
        )

    @property
    def request_source(self) -> str:
        return self.http_request.remote_addr

    @property
    def request_id(self) -> Union[str, int]:
        return self.json_rpc_request._id

    @property
    def method(self) -> str:
        return self.task.METHOD_NAME

    @property
    def _single_threaded(self):
        return self.task.args.single_threaded or SINGLE_THREADED_HANDLER

    def _wait_for_results(self) -> RemoteCallableResult:
        """Wait for results off the queue. If there is an exception raised,
        raise an appropriate RPC exception.

        This does not handle joining, but does terminate the process if it
        timed out.
        """
        if (
            self.subscriber is None or
            self.started is None or
            self.process is None
        ):
            raise dbt.exceptions.InternalException(
                '_wait_for_results() called before handle()'
            )

        try:
            msg = self.subscriber.dispatch_until_exit(
                started=self.started,
                timeout=self.timeout,
            )
        except dbt.exceptions.Exception as exc:
            raise dbt_error(exc)
        except Exception as exc:
            raise server_error(exc)
        if isinstance(msg, QueueErrorMessage):
            raise RPCException.from_error(msg.error)
        elif isinstance(msg, QueueTimeoutMessage):
            if not self._single_threaded:
                self.process.terminate()
            raise timeout_error(self.timeout)
        elif isinstance(msg, QueueResultMessage):
            return msg.result
        else:
            raise dbt.exceptions.InternalException(
                'Invalid message type {} (result={})'.format(msg)
            )

    @contextmanager
    def state_handler(self):
        try:
            try:
                yield
            finally:
                # make sure to set this _before_ updating state
                self.ended = datetime.utcnow()
        except RPCException as exc:
            self.error = exc
            self.state = TaskHandlerState.Error
            raise  # this re-raises for single-threaded operation
        except dbt.exceptions.Exception as exc:
            self.error = dbt_error(exc)
            self.state = TaskHandlerState.Error
            raise
        except BaseException as exc:
            # we should only get here if we got a BaseException that is not an
            # Exception (we caught those in _wait_for_results), or a bug in
            # get_result's call stack. Either way, we should set an error so we
            # can figure out what happened on thread death, and re-raise in
            # case it's something python-internal.
            self.error = server_error(exc)
            self.state = TaskHandlerState.Error
            raise
        else:
            self.state = TaskHandlerState.Success

    def get_result(self) -> RemoteCallableResult:
        if self.process is None:
            raise dbt.exceptions.InternalException(
                'get_result() called before handle()'
            )

        try:
            with list_handler(self.logs):
                try:
                    result = self._wait_for_results()
                finally:
                    if not self._single_threaded:
                        self.process.join()
        except RPCException as exc:
            # RPC Exceptions come already preserialized for the jsonrpc
            # framework
            exc.logs = [l.to_dict() for l in self.logs]
            raise

        # results get real logs
        result.logs = self.logs[:]
        return result

    def run(self):
        try:
            with self.state_handler():
                self.result = self.get_result()
        except RPCException:
            pass  # rpc exceptions are fine, the managing thread will handle it

    def handle_singlethreaded(self, kwargs):
        # in single-threaded mode, we're going to remain synchronous, so call
        # `run`, not `start`, and return an actual result.
        # note this shouldn't call self.run() as that has different semantics
        # (we want errors to raise)
        self.process.run()
        with self.state_handler():
            self.result = self.get_result()
        return self.result

    def start(self):
        # this is pretty unfortunate, but we have to reset the adapter
        # cache _before_ we fork on posix. libpq, but also any other
        # adapters that rely on file descriptors, get really messed up if
        # you fork(), because the fds get inherited but the state isn't
        # shared. The child process and the parent might end up trying to
        # do things on the same fd at the same time.
        # Also for some reason, if you do this after forking, even without
        # calling close(), the connection in the parent ends up throwing
        # 'connection already closed' exceptions
        cleanup_connections()
        self.process.start()
        self.state = TaskHandlerState.Running
        super().start()

    def handle(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        self.started = datetime.utcnow()
        self.state = TaskHandlerState.Initializing
        self.timeout = kwargs.pop('timeout', None)
        try:
            params = self.task.get_parameters().from_dict(kwargs)
        except ValidationError as exc:
            # raise a TypeError to indicate invalid parameters
            self.state = TaskHandlerState.Error
            raise TypeError(exc)
        self.subscriber = QueueSubscriber()
        self.process = multiprocessing.Process(
            target=_task_bootstrap,
            args=(self.task, self.subscriber.queue, params)
        )

        if self._single_threaded:
            # all requests are synchronous in single-threaded mode. No need to
            # create a process...
            return self.handle_singlethreaded(kwargs)

        self.start()
        return {'request_token': str(self.task_id)}

    def __call__(self, **kwargs) -> Dict[str, Any]:
        # __call__ happens deep inside jsonrpc's framework
        self.manager.add_request(self)
        return self.handle(kwargs)
