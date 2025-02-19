# Copyright 2016 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Service-side implementation of gRPC Python."""

import collections
import enum
import logging
import threading
import time

import six

import grpc
from grpc import _common
from grpc import _interceptor
from grpc._cython import cygrpc
from grpc.framework.foundation import callable_util

_SHUTDOWN_TAG = "shutdown"
_REQUEST_CALL_TAG = "request_call"

_RECEIVE_CLOSE_ON_SERVER_TOKEN = "receive_close_on_server"
_SEND_INITIAL_METADATA_TOKEN = "send_initial_metadata"
_RECEIVE_MESSAGE_TOKEN = "receive_message"
_SEND_MESSAGE_TOKEN = "send_message"
_SEND_INITIAL_METADATA_AND_SEND_MESSAGE_TOKEN = "send_initial_metadata * send_message"
_SEND_STATUS_FROM_SERVER_TOKEN = "send_status_from_server"
_SEND_INITIAL_METADATA_AND_SEND_STATUS_FROM_SERVER_TOKEN = (
    "send_initial_metadata * send_status_from_server"
)

_OPEN = "open"
_CLOSED = "closed"
_CANCELLED = "cancelled"

_EMPTY_FLAGS = 0

_UNEXPECTED_EXIT_SERVER_GRACE = 1.0


def _serialized_request(request_event):
    return request_event.batch_operations[0].message()


def _application_code(code):
    cygrpc_code = _common.STATUS_CODE_TO_CYGRPC_STATUS_CODE.get(code)
    return cygrpc.StatusCode.unknown if cygrpc_code is None else cygrpc_code


def _completion_code(state):
    if state.code is None:
        return cygrpc.StatusCode.ok
    else:
        return _application_code(state.code)


def _abortion_code(state, code):
    if state.code is None:
        return code
    else:
        return _application_code(state.code)


def _details(state):
    return b"" if state.details is None else state.details


class _HandlerCallDetails(
    collections.namedtuple("_HandlerCallDetails", ("method", "invocation_metadata",)),
    grpc.HandlerCallDetails,
):
    pass


class _RPCState(object):
    def __init__(self):
        self.condition = threading.Condition()
        self.due = set()
        self.request = None
        self.client = _OPEN
        self.initial_metadata_allowed = True
        self.disable_next_compression = False
        self.trailing_metadata = None
        self.code = None
        self.details = None
        self.statused = False
        self.rpc_errors = []
        self.callbacks = []
        self.abortion = None


def _raise_rpc_error(state):
    rpc_error = grpc.RpcError()
    state.rpc_errors.append(rpc_error)
    raise rpc_error


def _possibly_finish_call(state, token):
    state.due.remove(token)
    if (state.client is _CANCELLED or state.statused) and not state.due:
        callbacks = state.callbacks
        state.callbacks = None
        return state, callbacks
    else:
        return None, ()


def _send_status_from_server(state, token):
    def send_status_from_server(unused_send_status_from_server_event):
        with state.condition:
            return _possibly_finish_call(state, token)

    return send_status_from_server


def _abort(state, call, code, details):
    if state.client is not _CANCELLED:
        effective_code = _abortion_code(state, code)
        effective_details = details if state.details is None else state.details
        if state.initial_metadata_allowed:
            operations = (
                cygrpc.SendInitialMetadataOperation(None, _EMPTY_FLAGS),
                cygrpc.SendStatusFromServerOperation(
                    state.trailing_metadata,
                    effective_code,
                    effective_details,
                    _EMPTY_FLAGS,
                ),
            )
            token = _SEND_INITIAL_METADATA_AND_SEND_STATUS_FROM_SERVER_TOKEN
        else:
            operations = (
                cygrpc.SendStatusFromServerOperation(
                    state.trailing_metadata,
                    effective_code,
                    effective_details,
                    _EMPTY_FLAGS,
                ),
            )
            token = _SEND_STATUS_FROM_SERVER_TOKEN
        call.start_server_batch(operations, _send_status_from_server(state, token))
        state.statused = True
        state.due.add(token)


def _receive_close_on_server(state):
    def receive_close_on_server(receive_close_on_server_event):
        with state.condition:
            if receive_close_on_server_event.batch_operations[0].cancelled():
                state.client = _CANCELLED
            elif state.client is _OPEN:
                state.client = _CLOSED
            state.condition.notify_all()
            return _possibly_finish_call(state, _RECEIVE_CLOSE_ON_SERVER_TOKEN)

    return receive_close_on_server


def _receive_message(state, call, request_deserializer):
    def receive_message(receive_message_event):
        serialized_request = _serialized_request(receive_message_event)
        if serialized_request is None:
            with state.condition:
                if state.client is _OPEN:
                    state.client = _CLOSED
                state.condition.notify_all()
                return _possibly_finish_call(state, _RECEIVE_MESSAGE_TOKEN)
        else:
            request = _common.deserialize(serialized_request, request_deserializer)
            with state.condition:
                if request is None:
                    _abort(
                        state,
                        call,
                        cygrpc.StatusCode.internal,
                        b"Exception deserializing request!",
                    )
                else:
                    state.request = request
                state.condition.notify_all()
                return _possibly_finish_call(state, _RECEIVE_MESSAGE_TOKEN)

    return receive_message


def _send_initial_metadata(state):
    def send_initial_metadata(unused_send_initial_metadata_event):
        with state.condition:
            return _possibly_finish_call(state, _SEND_INITIAL_METADATA_TOKEN)

    return send_initial_metadata


def _send_message(state, token):
    def send_message(unused_send_message_event):
        with state.condition:
            state.condition.notify_all()
            return _possibly_finish_call(state, token)

    return send_message


class _Context(grpc.ServicerContext):
    def __init__(self, rpc_event, state, request_deserializer):
        self._rpc_event = rpc_event
        self._state = state
        self._request_deserializer = request_deserializer

    def is_active(self):
        with self._state.condition:
            return self._state.client is not _CANCELLED and not self._state.statused

    def time_remaining(self):
        return max(self._rpc_event.call_details.deadline - time.time(), 0)

    def cancel(self):
        self._rpc_event.call.cancel()

    def add_callback(self, callback):
        with self._state.condition:
            if self._state.callbacks is None:
                return False
            else:
                self._state.callbacks.append(callback)
                return True

    def disable_next_message_compression(self):
        with self._state.condition:
            self._state.disable_next_compression = True

    def invocation_metadata(self):
        return self._rpc_event.invocation_metadata

    def peer(self):
        return _common.decode(self._rpc_event.call.peer())

    def peer_identities(self):
        return cygrpc.peer_identities(self._rpc_event.call)

    def peer_identity_key(self):
        id_key = cygrpc.peer_identity_key(self._rpc_event.call)
        return id_key if id_key is None else _common.decode(id_key)

    def auth_context(self):
        return {
            _common.decode(key): value
            for key, value in six.iteritems(cygrpc.auth_context(self._rpc_event.call))
        }

    def send_initial_metadata(self, initial_metadata):
        with self._state.condition:
            if self._state.client is _CANCELLED:
                _raise_rpc_error(self._state)
            else:
                if self._state.initial_metadata_allowed:
                    operation = cygrpc.SendInitialMetadataOperation(
                        initial_metadata, _EMPTY_FLAGS
                    )
                    self._rpc_event.call.start_server_batch(
                        (operation,), _send_initial_metadata(self._state)
                    )
                    self._state.initial_metadata_allowed = False
                    self._state.due.add(_SEND_INITIAL_METADATA_TOKEN)
                else:
                    raise ValueError("Initial metadata no longer allowed!")

    def set_trailing_metadata(self, trailing_metadata):
        with self._state.condition:
            self._state.trailing_metadata = trailing_metadata

    def abort(self, code, details):
        # treat OK like other invalid arguments: fail the RPC
        if code == grpc.StatusCode.OK:
            logging.error("abort() called with StatusCode.OK; returning UNKNOWN")
            code = grpc.StatusCode.UNKNOWN
            details = ""
        with self._state.condition:
            self._state.code = code
            self._state.details = _common.encode(details)
            self._state.abortion = Exception()
            raise self._state.abortion

    def set_code(self, code):
        with self._state.condition:
            self._state.code = code

    def set_details(self, details):
        with self._state.condition:
            self._state.details = _common.encode(details)


class _RequestIterator(object):
    def __init__(self, state, call, request_deserializer):
        self._state = state
        self._call = call
        self._request_deserializer = request_deserializer

    def _raise_or_start_receive_message(self):
        if self._state.client is _CANCELLED:
            _raise_rpc_error(self._state)
        elif self._state.client is _CLOSED or self._state.statused:
            raise StopIteration()
        else:
            self._call.start_server_batch(
                (cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),),
                _receive_message(self._state, self._call, self._request_deserializer),
            )
            self._state.due.add(_RECEIVE_MESSAGE_TOKEN)

    def _look_for_request(self):
        if self._state.client is _CANCELLED:
            _raise_rpc_error(self._state)
        elif (
            self._state.request is None
            and _RECEIVE_MESSAGE_TOKEN not in self._state.due
        ):
            raise StopIteration()
        else:
            request = self._state.request
            self._state.request = None
            return request

    def _next(self):
        with self._state.condition:
            self._raise_or_start_receive_message()
            while True:
                self._state.condition.wait()
                request = self._look_for_request()
                if request is not None:
                    return request

    def __iter__(self):
        return self

    def __next__(self):
        return self._next()

    def next(self):
        return self._next()


def _unary_request(rpc_event, state, request_deserializer):
    def unary_request():
        with state.condition:
            if state.client is _CANCELLED or state.statused:
                return None
            else:
                rpc_event.call.start_server_batch(
                    (cygrpc.ReceiveMessageOperation(_EMPTY_FLAGS),),
                    _receive_message(state, rpc_event.call, request_deserializer),
                )
                state.due.add(_RECEIVE_MESSAGE_TOKEN)
                while True:
                    state.condition.wait()
                    if state.request is None:
                        if state.client is _CLOSED:
                            details = '"{}" requires exactly one request message.'.format(
                                rpc_event.call_details.method
                            )
                            _abort(
                                state,
                                rpc_event.call,
                                cygrpc.StatusCode.unimplemented,
                                _common.encode(details),
                            )
                            return None
                        elif state.client is _CANCELLED:
                            return None
                    else:
                        request = state.request
                        state.request = None
                        return request

    return unary_request


def _call_behavior(rpc_event, state, behavior, argument, request_deserializer):
    context = _Context(rpc_event, state, request_deserializer)
    try:
        return behavior(argument, context), True
    except Exception as exception:  # pylint: disable=broad-except
        with state.condition:
            if exception is state.abortion:
                _abort(state, rpc_event.call, cygrpc.StatusCode.unknown, b"RPC Aborted")
            elif exception not in state.rpc_errors:
                details = "Exception calling application: {}".format(exception)
                logging.exception(details)
                _abort(
                    state,
                    rpc_event.call,
                    cygrpc.StatusCode.unknown,
                    _common.encode(details),
                )
        return None, False


def _take_response_from_response_iterator(rpc_event, state, response_iterator):
    try:
        return next(response_iterator), True
    except StopIteration:
        return None, True
    except Exception as exception:  # pylint: disable=broad-except
        with state.condition:
            if exception is state.abortion:
                _abort(state, rpc_event.call, cygrpc.StatusCode.unknown, b"RPC Aborted")
            elif exception not in state.rpc_errors:
                details = "Exception iterating responses: {}".format(exception)
                logging.exception(details)
                _abort(
                    state,
                    rpc_event.call,
                    cygrpc.StatusCode.unknown,
                    _common.encode(details),
                )
        return None, False


def _serialize_response(rpc_event, state, response, response_serializer):
    serialized_response = _common.serialize(response, response_serializer)
    if serialized_response is None:
        with state.condition:
            _abort(
                state,
                rpc_event.call,
                cygrpc.StatusCode.internal,
                b"Failed to serialize response!",
            )
        return None
    else:
        return serialized_response


def _send_response(rpc_event, state, serialized_response):
    with state.condition:
        if state.client is _CANCELLED or state.statused:
            return False
        else:
            if state.initial_metadata_allowed:
                operations = (
                    cygrpc.SendInitialMetadataOperation(None, _EMPTY_FLAGS),
                    cygrpc.SendMessageOperation(serialized_response, _EMPTY_FLAGS),
                )
                state.initial_metadata_allowed = False
                token = _SEND_INITIAL_METADATA_AND_SEND_MESSAGE_TOKEN
            else:
                operations = (
                    cygrpc.SendMessageOperation(serialized_response, _EMPTY_FLAGS),
                )
                token = _SEND_MESSAGE_TOKEN
            rpc_event.call.start_server_batch(operations, _send_message(state, token))
            state.due.add(token)
            while True:
                state.condition.wait()
                if token not in state.due:
                    return state.client is not _CANCELLED and not state.statused


def _status(rpc_event, state, serialized_response):
    with state.condition:
        if state.client is not _CANCELLED:
            code = _completion_code(state)
            details = _details(state)
            operations = [
                cygrpc.SendStatusFromServerOperation(
                    state.trailing_metadata, code, details, _EMPTY_FLAGS
                ),
            ]
            if state.initial_metadata_allowed:
                operations.append(
                    cygrpc.SendInitialMetadataOperation(None, _EMPTY_FLAGS)
                )
            if serialized_response is not None:
                operations.append(
                    cygrpc.SendMessageOperation(serialized_response, _EMPTY_FLAGS)
                )
            rpc_event.call.start_server_batch(
                operations,
                _send_status_from_server(state, _SEND_STATUS_FROM_SERVER_TOKEN),
            )
            state.statused = True
            state.due.add(_SEND_STATUS_FROM_SERVER_TOKEN)


def _unary_response_in_pool(
    rpc_event,
    state,
    behavior,
    argument_thunk,
    request_deserializer,
    response_serializer,
):
    argument = argument_thunk()
    if argument is not None:
        response, proceed = _call_behavior(
            rpc_event, state, behavior, argument, request_deserializer
        )
        if proceed:
            serialized_response = _serialize_response(
                rpc_event, state, response, response_serializer
            )
            if serialized_response is not None:
                _status(rpc_event, state, serialized_response)


def _stream_response_in_pool(
    rpc_event,
    state,
    behavior,
    argument_thunk,
    request_deserializer,
    response_serializer,
):
    argument = argument_thunk()
    if argument is not None:
        response_iterator, proceed = _call_behavior(
            rpc_event, state, behavior, argument, request_deserializer
        )
        if proceed:
            while True:
                response, proceed = _take_response_from_response_iterator(
                    rpc_event, state, response_iterator
                )
                if proceed:
                    if response is None:
                        _status(rpc_event, state, None)
                        break
                    else:
                        serialized_response = _serialize_response(
                            rpc_event, state, response, response_serializer
                        )
                        if serialized_response is not None:
                            proceed = _send_response(
                                rpc_event, state, serialized_response
                            )
                            if not proceed:
                                break
                        else:
                            break
                else:
                    break


def _handle_unary_unary(rpc_event, state, method_handler, thread_pool):
    unary_request = _unary_request(
        rpc_event, state, method_handler.request_deserializer
    )
    return thread_pool.submit(
        _unary_response_in_pool,
        rpc_event,
        state,
        method_handler.unary_unary,
        unary_request,
        method_handler.request_deserializer,
        method_handler.response_serializer,
    )


def _handle_unary_stream(rpc_event, state, method_handler, thread_pool):
    unary_request = _unary_request(
        rpc_event, state, method_handler.request_deserializer
    )
    return thread_pool.submit(
        _stream_response_in_pool,
        rpc_event,
        state,
        method_handler.unary_stream,
        unary_request,
        method_handler.request_deserializer,
        method_handler.response_serializer,
    )


def _handle_stream_unary(rpc_event, state, method_handler, thread_pool):
    request_iterator = _RequestIterator(
        state, rpc_event.call, method_handler.request_deserializer
    )
    return thread_pool.submit(
        _unary_response_in_pool,
        rpc_event,
        state,
        method_handler.stream_unary,
        lambda: request_iterator,
        method_handler.request_deserializer,
        method_handler.response_serializer,
    )


def _handle_stream_stream(rpc_event, state, method_handler, thread_pool):
    request_iterator = _RequestIterator(
        state, rpc_event.call, method_handler.request_deserializer
    )
    return thread_pool.submit(
        _stream_response_in_pool,
        rpc_event,
        state,
        method_handler.stream_stream,
        lambda: request_iterator,
        method_handler.request_deserializer,
        method_handler.response_serializer,
    )


def _find_method_handler(rpc_event, generic_handlers, interceptor_pipeline):
    def query_handlers(handler_call_details):
        for generic_handler in generic_handlers:
            method_handler = generic_handler.service(handler_call_details)
            if method_handler is not None:
                return method_handler
        return None

    handler_call_details = _HandlerCallDetails(
        _common.decode(rpc_event.call_details.method), rpc_event.invocation_metadata
    )

    if interceptor_pipeline is not None:
        return interceptor_pipeline.execute(query_handlers, handler_call_details)
    else:
        return query_handlers(handler_call_details)


def _reject_rpc(rpc_event, status, details):
    operations = (
        cygrpc.SendInitialMetadataOperation(None, _EMPTY_FLAGS),
        cygrpc.ReceiveCloseOnServerOperation(_EMPTY_FLAGS),
        cygrpc.SendStatusFromServerOperation(None, status, details, _EMPTY_FLAGS),
    )
    rpc_state = _RPCState()
    rpc_event.call.start_server_batch(
        operations, lambda ignored_event: (rpc_state, (),)
    )
    return rpc_state


def _handle_with_method_handler(rpc_event, method_handler, thread_pool):
    state = _RPCState()
    with state.condition:
        rpc_event.call.start_server_batch(
            (cygrpc.ReceiveCloseOnServerOperation(_EMPTY_FLAGS),),
            _receive_close_on_server(state),
        )
        state.due.add(_RECEIVE_CLOSE_ON_SERVER_TOKEN)
        if method_handler.request_streaming:
            if method_handler.response_streaming:
                return (
                    state,
                    _handle_stream_stream(
                        rpc_event, state, method_handler, thread_pool
                    ),
                )
            else:
                return (
                    state,
                    _handle_stream_unary(rpc_event, state, method_handler, thread_pool),
                )
        else:
            if method_handler.response_streaming:
                return (
                    state,
                    _handle_unary_stream(rpc_event, state, method_handler, thread_pool),
                )
            else:
                return (
                    state,
                    _handle_unary_unary(rpc_event, state, method_handler, thread_pool),
                )


def _handle_call(
    rpc_event, generic_handlers, interceptor_pipeline, thread_pool, concurrency_exceeded
):
    if not rpc_event.success:
        return None, None
    if rpc_event.call_details.method is not None:
        try:
            method_handler = _find_method_handler(
                rpc_event, generic_handlers, interceptor_pipeline
            )
        except Exception as exception:  # pylint: disable=broad-except
            details = "Exception servicing handler: {}".format(exception)
            logging.exception(details)
            return (
                _reject_rpc(
                    rpc_event, cygrpc.StatusCode.unknown, b"Error in service handler!"
                ),
                None,
            )
        if method_handler is None:
            return (
                _reject_rpc(
                    rpc_event, cygrpc.StatusCode.unimplemented, b"Method not found!"
                ),
                None,
            )
        elif concurrency_exceeded:
            return (
                _reject_rpc(
                    rpc_event,
                    cygrpc.StatusCode.resource_exhausted,
                    b"Concurrent RPC limit exceeded!",
                ),
                None,
            )
        else:
            return _handle_with_method_handler(rpc_event, method_handler, thread_pool)
    else:
        return None, None


@enum.unique
class _ServerStage(enum.Enum):
    STOPPED = "stopped"
    STARTED = "started"
    GRACE = "grace"


class _ServerState(object):

    # pylint: disable=too-many-arguments
    def __init__(
        self,
        completion_queue,
        server,
        generic_handlers,
        interceptor_pipeline,
        thread_pool,
        maximum_concurrent_rpcs,
    ):
        self.lock = threading.RLock()
        self.completion_queue = completion_queue
        self.server = server
        self.generic_handlers = list(generic_handlers)
        self.interceptor_pipeline = interceptor_pipeline
        self.thread_pool = thread_pool
        self.stage = _ServerStage.STOPPED
        self.shutdown_events = None
        self.maximum_concurrent_rpcs = maximum_concurrent_rpcs
        self.active_rpc_count = 0

        # TODO(https://github.com/grpc/grpc/issues/6597): eliminate these fields.
        self.rpc_states = set()
        self.due = set()


def _add_generic_handlers(state, generic_handlers):
    with state.lock:
        state.generic_handlers.extend(generic_handlers)


def _add_insecure_port(state, address):
    with state.lock:
        return state.server.add_http2_port(address)


def _add_secure_port(state, address, server_credentials):
    with state.lock:
        return state.server.add_http2_port(address, server_credentials._credentials)


def _request_call(state):
    state.server.request_call(
        state.completion_queue, state.completion_queue, _REQUEST_CALL_TAG
    )
    state.due.add(_REQUEST_CALL_TAG)


# TODO(https://github.com/grpc/grpc/issues/6597): delete this function.
def _stop_serving(state):
    if not state.rpc_states and not state.due:
        for shutdown_event in state.shutdown_events:
            shutdown_event.set()
        state.stage = _ServerStage.STOPPED
        return True
    else:
        return False


def _on_call_completed(state):
    with state.lock:
        state.active_rpc_count -= 1


def _serve(state):
    while True:
        event = state.completion_queue.poll()
        if event.tag is _SHUTDOWN_TAG:
            with state.lock:
                state.due.remove(_SHUTDOWN_TAG)
                if _stop_serving(state):
                    return
        elif event.tag is _REQUEST_CALL_TAG:
            with state.lock:
                state.due.remove(_REQUEST_CALL_TAG)
                concurrency_exceeded = (
                    state.maximum_concurrent_rpcs is not None
                    and state.active_rpc_count >= state.maximum_concurrent_rpcs
                )
                rpc_state, rpc_future = _handle_call(
                    event,
                    state.generic_handlers,
                    state.interceptor_pipeline,
                    state.thread_pool,
                    concurrency_exceeded,
                )
                if rpc_state is not None:
                    state.rpc_states.add(rpc_state)
                if rpc_future is not None:
                    state.active_rpc_count += 1
                    rpc_future.add_done_callback(
                        lambda unused_future: _on_call_completed(state)
                    )
                if state.stage is _ServerStage.STARTED:
                    _request_call(state)
                elif _stop_serving(state):
                    return
        else:
            rpc_state, callbacks = event.tag(event)
            for callback in callbacks:
                callable_util.call_logging_exceptions(
                    callback, "Exception calling callback!"
                )
            if rpc_state is not None:
                with state.lock:
                    state.rpc_states.remove(rpc_state)
                    if _stop_serving(state):
                        return
        # We want to force the deletion of the previous event
        # ~before~ we poll again; if the event has a reference
        # to a shutdown Call object, this can induce spinlock.
        event = None


def _stop(state, grace):
    with state.lock:
        if state.stage is _ServerStage.STOPPED:
            shutdown_event = threading.Event()
            shutdown_event.set()
            return shutdown_event
        else:
            if state.stage is _ServerStage.STARTED:
                state.server.shutdown(state.completion_queue, _SHUTDOWN_TAG)
                state.stage = _ServerStage.GRACE
                state.shutdown_events = []
                state.due.add(_SHUTDOWN_TAG)
            shutdown_event = threading.Event()
            state.shutdown_events.append(shutdown_event)
            if grace is None:
                state.server.cancel_all_calls()
            else:

                def cancel_all_calls_after_grace():
                    shutdown_event.wait(timeout=grace)
                    with state.lock:
                        state.server.cancel_all_calls()

                thread = threading.Thread(target=cancel_all_calls_after_grace)
                thread.start()
                return shutdown_event
    shutdown_event.wait()
    return shutdown_event


def _start(state):
    with state.lock:
        if state.stage is not _ServerStage.STOPPED:
            raise ValueError("Cannot start already-started server!")
        state.server.start()
        state.stage = _ServerStage.STARTED
        _request_call(state)

        def cleanup_server(timeout):
            if timeout is None:
                _stop(state, _UNEXPECTED_EXIT_SERVER_GRACE).wait()
            else:
                _stop(state, timeout).wait()

        thread = _common.CleanupThread(cleanup_server, target=_serve, args=(state,))
        thread.start()


class Server(grpc.Server):

    # pylint: disable=too-many-arguments
    def __init__(
        self,
        thread_pool,
        generic_handlers,
        interceptors,
        options,
        maximum_concurrent_rpcs,
    ):
        completion_queue = cygrpc.CompletionQueue()
        server = cygrpc.Server(options)
        server.register_completion_queue(completion_queue)
        self._state = _ServerState(
            completion_queue,
            server,
            generic_handlers,
            _interceptor.service_pipeline(interceptors),
            thread_pool,
            maximum_concurrent_rpcs,
        )

    def add_generic_rpc_handlers(self, generic_rpc_handlers):
        _add_generic_handlers(self._state, generic_rpc_handlers)

    def add_insecure_port(self, address):
        return _add_insecure_port(self._state, _common.encode(address))

    def add_secure_port(self, address, server_credentials):
        return _add_secure_port(
            self._state, _common.encode(address), server_credentials
        )

    def start(self):
        _start(self._state)

    def stop(self, grace):
        return _stop(self._state, grace)

    def __del__(self):
        _stop(self._state, None)
