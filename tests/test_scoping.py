import pytest

from tierkreis.frontend import RuntimeClient, ServerRuntime
from tierkreis.frontend.builder import Const, Copyable, Output, Scope, graph


@pytest.mark.asyncio
async def test_run_scoped_program(bi, client: RuntimeClient) -> None:
    @graph()
    def g() -> Output:
        a = Const(3)
        with Scope():
            b = Copyable(Const(2))
            with Scope():
                c = bi.iadd(a, b)
            d = bi.iadd(c, b)
        e = bi.iadd(d, Const(1))
        return Output(value=e)

    outputs = await client.run_graph(g())
    assert outputs["value"].try_autopython() == 8


@pytest.mark.asyncio
async def test_remote_scopes(server_client: ServerRuntime, local_runtime_launcher, bi):
    async with local_runtime_launcher(
        grpc_port=9090,
        worker_uris=[("inner", "http://" + server_client.socket_address())],
        show_output=True,
    ) as outer:

        @graph()
        def g() -> Output:
            a = Copyable(Const(3))
            with Scope("inner"):
                b = Const(2)
                c = bi.iadd(b, a)
            d = bi.iadd(a, c)
            return Output(value=d)

        outputs = await outer.run_graph(g())
        assert outputs["value"].try_autopython() == 8


@pytest.mark.asyncio
async def test_remote_scopes_are_actually_remote_control(
    server_client: ServerRuntime, local_runtime_launcher, bi
):
    async with local_runtime_launcher(
        grpc_port=9090,
        worker_uris=[("inner", "http://" + server_client.socket_address())],
    ) as outer:

        @graph()
        def g() -> Output:
            with Scope("inner"):
                bi["python_nodes"].id_py(Const(1))
            return Output()

        await outer.run_graph(g())


@pytest.mark.asyncio
async def test_remote_scopes_are_actually_remote(local_runtime_launcher, bi):
    async with local_runtime_launcher(
        grpc_port=8081,
        workers=[],
    ) as inner:
        async with local_runtime_launcher(
            grpc_port=9090,
            worker_uris=[("inner", "http://" + inner.socket_address())],
        ) as outer:

            @graph()
            def g() -> Output:
                with Scope("inner"):
                    bi["python_nodes"].id_py(Const(1))
                return Output()

            with pytest.raises(RuntimeError) as err:
                await outer.run_graph(g())
            assert "unknown function python_nodes::id_py" in str(err)


@pytest.mark.asyncio
async def test_worker_scopes(server_client: ServerRuntime, bi):
    @graph()
    def g() -> Output:
        with Scope("python"):
            x = bi["python_nodes"].id_py(Const(1))
        return Output(value=x)

    outputs = await server_client.run_graph(g())
    assert outputs["value"].try_autopython() == 1
