"""Worker server implementation."""

import sys
from tempfile import TemporaryDirectory
from typing import Any, Awaitable, Callable, Coroutine, Optional, cast

import grpclib
import grpclib.events
import grpclib.server
import keyring
from grpclib.const import Status as StatusCode
from grpclib.exceptions import GRPCError
from grpclib.server import Server

import tierkreis.core.protos.tierkreis.graph as pg
import tierkreis.core.protos.tierkreis.runtime as pr
import tierkreis.core.protos.tierkreis.signature as ps
import tierkreis.core.protos.tierkreis.worker as pw
from tierkreis.core.function import FunctionName
from tierkreis.core.protos.tierkreis.worker import RunFunctionResponse, WorkerBase
from tierkreis.core.python_runtime import PyRuntime
from tierkreis.core.tierkreis_graph import TierkreisGraph
from tierkreis.core.types import TierkreisTypeErrors
from tierkreis.core.values import StructValue

from .callback import CallbackHook
from .exceptions import (
    DecodeInputError,
    EncodeOutputError,
    FunctionNotFound,
    NodeExecutionError,
)
from .namespace import Namespace
from .tracing import _TRACING, context_token, get_tracer, span

if _TRACING:
    import opentelemetry.context
    import opentelemetry.propagate
    import opentelemetry.trace
    from opentelemetry.semconv.trace import SpanAttributes


tracer = get_tracer(__name__)


async def _event_recv_request(request: grpclib.events.RecvRequest):
    method_func = request.method_func
    service, method = request.method_name.lstrip("/").split("/", 1)
    kwargs: dict[str, Any] = dict(name=f"GRPC: {request.method_name}")
    if _TRACING:
        context = opentelemetry.propagate.extract(request.metadata)
        attributes = {
            SpanAttributes.RPC_SYSTEM: "grpc",
            SpanAttributes.RPC_SERVICE: service,
            SpanAttributes.RPC_METHOD: method,
        }
        kwargs["kind"] = opentelemetry.trace.SpanKind.SERVER
        kwargs["attributes"] = attributes
    else:
        context = None

    async def wrapped(stream: grpclib.server.Stream):
        with context_token(context):
            with span(tracer, **kwargs) as s:
                from .prelude import profile_worker  # avoid cyclic import

                if profile_worker:
                    import cProfile  # could be global
                    import pstats
                    from io import StringIO

                    render_results = StringIO()
                    with cProfile.Profile() as pr:
                        # If there are multiple requests being processed at the
                        # same time, due to "async" this will likely get confused,
                        # i.e. combine processing for any+all of them together.
                        pr.enable()
                        await method_func(stream)
                        pr.disable()
                        stats = pstats.Stats(pr, stream=render_results)
                    stats.sort_stats("cumtime")
                    stats.print_stats(20)  # Top 20 lines
                    render_results.seek(0)
                    s.set_attribute("profile_results", render_results.read())
                else:
                    await method_func(stream)

    request.method_func = wrapped


_KEYRING_SERVICE = "tierkreis_extracted"


class Worker:
    """Worker server."""

    root: Namespace
    server: Server
    callback_hook: CallbackHook
    pyruntime: PyRuntime

    def __init__(self, callback_hook, root_namespace):
        self.root = root_namespace
        self.callback_hook = callback_hook
        self.pyruntime = PyRuntime([root_namespace])
        self.server = Server(
            [SignatureServerImpl(self), WorkerServerImpl(self), RuntimeServerImpl(self)]
        )

        # Attach event listener for tracing
        self._add_request_listener(_event_recv_request)
        # Attach event listener to pick up callback address
        self._add_request_listener(self._extract_callback)
        # Attach event listener to extract auth credentials
        self._add_request_listener(self._extract_auth)

    def run(
        self, function: FunctionName, inputs: StructValue
    ) -> Awaitable[StructValue]:
        """Run function."""
        func = self.root.get_function(function)
        if func is None:
            raise FunctionNotFound(function)

        # See https://github.com/python/mypy/issues/5485
        return cast(Any, func).run(inputs)

    async def _extract_callback(self, request: grpclib.events.RecvRequest):
        if self.callback_hook.callback is None:
            callback_host = str(request.metadata.get("tierkreis_callback_host"))
            callback_port = int(request.metadata.get("tierkreis_callback_port"))  # type: ignore

            self.callback_hook.callback = (callback_host, callback_port)

    async def _extract_auth(self, request: grpclib.events.RecvRequest) -> None:
        if keyring.get_password(_KEYRING_SERVICE, "token") is None:
            token = request.metadata.pop("token", None)
            key = request.metadata.pop("key", None)
            if (token is not None) and (key is not None):
                keyring.set_password(_KEYRING_SERVICE, "token", str(token))
                keyring.set_password(_KEYRING_SERVICE, "key", str(key))

    def _add_request_listener(
        self,
        listener: Callable[[grpclib.events.RecvRequest], Coroutine[Any, Any, None]],
    ):
        grpclib.events.listen(self.server, grpclib.events.RecvRequest, listener)

    async def start(self, port: Optional[int] = None):
        """Start server."""

        if port:
            await self.server.start(port=port)

            print(f"Started worker server on port: {port}", flush=True)
            await self.server.wait_closed()

        else:
            with TemporaryDirectory() as socket_dir:
                # Create a temporary path for a unix domain socket.
                socket_path = f"{socket_dir}/python_worker.sock"

                # Start the python worker gRPC server and bind to the unix domain socket
                await self.server.start(path=socket_path)

                # Print the path of the unix domain socket to stdout so the runtime can
                # connect to it. Without the flush the runtime did not receive the
                # socket path and blocked indefinitely.
                print(socket_path)
                sys.stdout.flush()

                await self.server.wait_closed()

        # clear any stored credentials
        keyring.delete_password(_KEYRING_SERVICE, "token")
        keyring.delete_password(_KEYRING_SERVICE, "key")


class WorkerServerImpl(WorkerBase):
    worker: Worker

    def __init__(self, worker: Worker):
        self.worker = worker

    async def run_function(
        self,
        run_function_request: pw.RunFunctionRequest,
    ) -> RunFunctionResponse:
        function = run_function_request.function
        inputs = run_function_request.inputs
        try:
            function_name = FunctionName.from_proto(function)
            inputs_struct = StructValue.from_proto_dict(inputs.map)
            outputs_struct = await self.worker.run(function_name, inputs_struct)
            with span(tracer, name="encoding python type in RunFunctionResponse proto"):
                res = RunFunctionResponse(
                    outputs=pg.StructValue(outputs_struct.to_proto_dict())
                )
            return res
        except DecodeInputError as err:
            raise GRPCError(
                status=StatusCode.INVALID_ARGUMENT,
                message=f"Error while decoding inputs: {err}",
            ) from err
        except EncodeOutputError as err:
            raise GRPCError(
                status=StatusCode.INTERNAL,
                message=f"Error while encoding outputs: {err}",
            ) from err
        except FunctionNotFound as err:
            raise GRPCError(
                status=StatusCode.UNIMPLEMENTED,
                message=f"Unsupported function: {function}",
            ) from err
        except NodeExecutionError as err:
            raise GRPCError(
                status=StatusCode.UNKNOWN,
                message=f"Error while running operation: {repr(err.base_exception)}",
            ) from err


class SignatureServerImpl(ps.SignatureBase):
    worker: Worker

    def __init__(self, worker: Worker):
        self.worker = worker

    async def list_functions(
        self, _: ps.ListFunctionsRequest
    ) -> ps.ListFunctionsResponse:
        signature = self.worker.root.extract_signature(True)

        return signature.to_proto()


class RuntimeServerImpl(pr.RuntimeBase):
    worker: Worker

    def __init__(self, worker: Worker):
        self.worker = worker

    async def run_graph(
        self, run_graph_request: pr.RunGraphRequest
    ) -> pr.RunGraphResponse:
        graph = TierkreisGraph.from_proto(run_graph_request.graph)
        inputs = StructValue.from_proto_dict(run_graph_request.inputs.map)
        try:
            if run_graph_request.type_check:
                (
                    graph,
                    inputs,
                ) = await self.worker.pyruntime.type_check_graph_with_inputs(
                    graph, inputs
                )

            outputs = await self.worker.pyruntime.run_graph(graph, **inputs.values)
            return pr.RunGraphResponse(
                success=pg.StructValue(map=StructValue(outputs).to_proto_dict())
            )
        except TierkreisTypeErrors as err:
            return pr.RunGraphResponse(type_errors=err.to_proto())
        except Exception as err:
            return pr.RunGraphResponse(error=str(err))
