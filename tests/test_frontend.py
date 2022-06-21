# pylint: disable=redefined-outer-name, missing-docstring, invalid-name
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type
from time import time

import pytest
from tierkreis import TierkreisGraph
from tierkreis.core.function import TierkreisFunction
from tierkreis.core.tierkreis_graph import FunctionNode, GraphValue, NodePort
from tierkreis.core.tierkreis_struct import TierkreisStruct
from tierkreis.core.types import (
    FloatType,
    GraphType,
    IntType,
    PairType,
    Row,
    StarKind,
    TierkreisTypeErrors,
    TypeScheme,
    VarType,
)
from tierkreis.core.values import FloatValue, StructValue, VariantValue
from tierkreis.frontend import RuntimeClient
from tierkreis.frontend.tksl import load_tksl_file
from tierkreis.frontend.type_inference import infer_graph_types

from . import REASON, release_tests


def nint_adder(number: int, client: RuntimeClient) -> TierkreisGraph:
    tk_g = TierkreisGraph()
    current_outputs = tk_g.vec_last_n_elems(tk_g.input["array"], number)

    while len(current_outputs) > 1:
        next_outputs = []
        n_even = len(current_outputs) & ~1

        for i in range(0, n_even, 2):
            nod = tk_g.add_func(
                "builtin/iadd",
                a=current_outputs[i],
                b=current_outputs[i + 1],
            )
            next_outputs.append(nod["value"])
        if len(current_outputs) > n_even:
            nod = tk_g.add_func(
                "builtin/iadd",
                a=next_outputs[-1],
                b=current_outputs[n_even],
            )
            next_outputs[-1] = nod["value"]
        current_outputs = next_outputs

    tk_g.set_outputs(out=current_outputs[0])

    return tk_g


@pytest.mark.asyncio
async def test_mistyped_op(client: RuntimeClient):
    tk_g = TierkreisGraph()
    nod = tk_g.add_func("python_nodes/mistyped_op", inp=tk_g.input["testinp"])
    tk_g.set_outputs(out=nod)
    with pytest.raises(RuntimeError, match="Type mismatch"):
        await client.run_graph(tk_g, {"testinp": 3})


@pytest.mark.asyncio
@pytest.mark.skipif(release_tests, reason=REASON)
async def test_mistyped_op_nochecks(local_runtime_launcher):
    async with local_runtime_launcher(
        grpc_port=9090,
        env_vars={"TIERKREIS_DISABLE_RUNTIME_CHECKS": "1"},
    ) as server:
        tk_g = TierkreisGraph()
        nod = tk_g.add_func("python_nodes/mistyped_op", inp=tk_g.input["testinp"])
        tk_g.set_outputs(out=nod)
        res = await server.run_graph(tk_g, {"testinp": 3})
        assert res["out"].try_autopython() == 4.1


@pytest.mark.asyncio
async def test_nint_adder(client: RuntimeClient):

    tksl_g = load_tksl_file(
        Path(__file__).parent / "tksl_samples/nint_adder.tksl",
        signature=await client.get_signature(),
    )

    for in_list in ([1] * 5, list(range(5))):
        tk_g = nint_adder(len(in_list), client)
        outputs = await client.run_graph(tk_g, {"array": in_list})
        assert outputs["out"].try_autopython() == sum(in_list)

        tksl_outs = await client.run_graph(
            tksl_g, {"array": in_list, "len": len(in_list)}
        )
        assert tksl_outs["out"].try_autopython() == sum(in_list)


def add_n_graph(increment: int) -> TierkreisGraph:
    tk_g = TierkreisGraph()

    add_func = tk_g.add_func("builtin/iadd", a=increment, b=tk_g.input["number"])
    tk_g.set_outputs(output=add_func)

    return tk_g


@pytest.mark.asyncio
async def test_switch(client: RuntimeClient):
    add_2_g = add_n_graph(2)
    add_3_g = add_n_graph(3)
    tk_g = TierkreisGraph()
    sig = await client.get_signature()

    switch = tk_g.add_func(
        sig["builtin"].functions["switch"],
        if_true=tk_g.add_const(add_2_g),
        if_false=tk_g.add_const(add_3_g),
        pred=tk_g.input["flag"],
    )

    eval_node = tk_g.add_func(
        sig["builtin"].functions["eval"], thunk=switch, number=tk_g.input["number"]
    )

    tk_g.set_outputs(out=eval_node["output"])

    outs = await client.run_graph(tk_g, {"flag": True, "number": 3})

    assert outs["out"].try_autopython() == 5
    outs = await client.run_graph(tk_g, {"flag": False, "number": 3})

    assert outs["out"].try_autopython() == 6


@pytest.mark.asyncio
async def test_match(client: RuntimeClient):
    # Test a variant type < one: Float | many: Vec<Float> >
    one_graph = TierkreisGraph()
    one_graph.set_outputs(
        value=one_graph.add_func("builtin/fadd", a=one_graph.input["value"], b=3.14)
    )
    many_graph = TierkreisGraph()
    many_graph.set_outputs(
        value=many_graph.add_func("builtin/pop", vec=many_graph.input["value"])["item"]
    )

    match_graph = TierkreisGraph()
    match_graph.set_outputs(
        result=match_graph.add_func(
            "python_nodes/id_delay",
            wait=1,
            value=match_graph.add_func(
                "builtin/eval",
                thunk=match_graph.add_match(
                    match_graph.input["vv"],
                    one=match_graph.add_const(one_graph),
                    many=match_graph.add_func(
                        "python_nodes/id_delay",
                        wait=1,
                        value=match_graph.add_const(many_graph),
                    ),
                )["thunk"],
            ),
        )
    )

    start_time = time()
    outs = await client.run_graph(
        match_graph, {"vv": VariantValue("one", FloatValue(6.0))}
    )
    time_taken = time() - start_time
    assert outs["result"] == FloatValue(9.14)
    # Must have waited at least 1s for the delay on the graph output.
    assert time_taken >= 1.0
    # Should not have had to wait 1s first for the "many" graph input to be ready,
    # as that was not the variant was not selected.
    # (1.5s is arbitrary, but less than 2s.)
    assert time_taken < 1.5


@pytest.mark.asyncio
async def test_tag(client: RuntimeClient):
    g = TierkreisGraph()
    g.set_outputs(res=g.add_tag("foo", value=g.input["arg"]))
    v = FloatValue(67.1)
    outs = await client.run_graph(g, {"arg": v})
    assert outs == {"res": VariantValue("foo", v)}


@dataclass
class NestedStruct(TierkreisStruct):
    s: List[int]
    a: Tuple[int, bool]
    b: Optional[str]
    d: Optional[float]


@dataclass
class TstStruct(TierkreisStruct):
    x: int
    y: bool
    m: Dict[int, int]
    n: NestedStruct


def idpy_graph() -> TierkreisGraph:
    tk_g = TierkreisGraph()

    id_node = tk_g.add_func("python_nodes/id_py", value=tk_g.input["id_in"])
    tk_g.set_outputs(id_out=id_node)

    return tk_g


@pytest.mark.asyncio
async def test_idpy(client: RuntimeClient):
    async def assert_id_py(val: Any, typ: Type) -> bool:
        tk_g = idpy_graph()
        output = await client.run_graph(tk_g, {"id_in": val})
        val_decoded = output["id_out"].to_python(typ)
        return val_decoded == val

    dic: Dict[int, bool] = {1: True, 2: False}

    nestst = NestedStruct([1, 2, 3], (5, True), "asdf", None)
    testst = TstStruct(2, False, {66: 77}, nestst)
    pairs: list[tuple[Any, Type]] = [
        (dic, dict[int, bool]),
        (testst, TstStruct),
        ("test123", str),
        (2, int),
        (132.3, float),
        ((2, "a"), tuple[int, str]),  # type: ignore
        ([1, 2, 3], list[int]),
        (True, bool),
    ]
    for val, typ in pairs:
        assert await assert_id_py(val, typ)


@pytest.mark.asyncio
async def test_infer(client: RuntimeClient) -> None:
    # test when built with client types are auto inferred
    tg = TierkreisGraph()
    _, val1 = tg.copy_value(3)
    tg.set_outputs(out=val1)
    tg = await client.type_check_graph(tg)
    assert any(node.is_discard_node() for node in tg.nodes().values())

    assert isinstance(tg.get_edge(val1, NodePort(tg.output, "out")).type_, IntType)


@pytest.mark.asyncio
async def test_infer_errors(client: RuntimeClient) -> None:
    # build graph with two type errors
    tg = TierkreisGraph()
    node_0 = tg.add_const(0)
    node_1 = tg.add_const(1)
    tg.add_edge(node_0["value"], tg.input["illegal"])
    tg.set_outputs(out=node_1["invalid"])

    with pytest.raises(TierkreisTypeErrors) as err:
        await client.type_check_graph(tg)

    assert len(err.value) == 2


@pytest.mark.asyncio
async def test_infer_errors_when_running(client: RuntimeClient) -> None:
    # build graph with two type errors
    tg = TierkreisGraph()
    node_0 = tg.add_const(0)
    node_1 = tg.add_const(1)
    tg.add_edge(node_0["value"], tg.input["illegal"])
    tg.set_outputs(out=node_1["invalid"])

    with pytest.raises(TierkreisTypeErrors) as err:
        await client.run_graph(tg, {})

    assert len(err.value) == 2


@pytest.mark.asyncio
async def test_fail_node(client: RuntimeClient) -> None:
    tg = TierkreisGraph()
    tg.add_func("python_nodes/fail")

    with pytest.raises(RuntimeError) as err:
        await client.run_graph(tg, {})

    assert "fail node was run" in str(err.value)


def graph_from_func(func: TierkreisFunction) -> TierkreisGraph:
    # build a graph with a single function node, with the same interface as that
    # function
    tg = TierkreisGraph()
    node = tg.add_func(func.name, **{port: tg.input[port] for port in func.input_order})
    tg.set_outputs(**{port: node[port] for port in func.output_order})
    return tg


@pytest.mark.asyncio
async def test_vec_sequence(client: RuntimeClient) -> None:
    sig = await client.get_signature()
    pop_g = graph_from_func(sig["builtin"].functions["pop"])
    push_g = graph_from_func(sig["builtin"].functions["push"])

    seq_g = graph_from_func(sig["builtin"].functions["sequence"])

    outputs = await client.run_graph(seq_g, {"first": pop_g, "second": push_g})

    sequenced_g = outputs["sequenced"].to_python(TierkreisGraph).inline_boxes()

    # check composition is succesful
    functions = {
        node.function_name
        for node in sequenced_g.nodes().values()
        if isinstance(node, FunctionNode)
    }
    assert {"builtin/push", "builtin/pop"}.issubset(functions)

    # check composition evaluates
    vec_in = ["foo", "bar", "bang"]
    out = await client.run_graph(sequenced_g, {"vec": vec_in})
    vec_out = out["vec"].to_python(List[str])
    assert vec_in == vec_out


@pytest.fixture
def mock_myqos_creds(monkeypatch):
    """Inject mock credentials, as the worker server is not set to authenticate,
    these will not actually be checked."""
    monkeypatch.setenv("TIERKREIS_MYQOS_TOKEN", "TestToken")
    monkeypatch.setenv("TIERKREIS_MYQOS_KEY", "TestKey")


@pytest.mark.asyncio
@pytest.mark.skipif(release_tests, reason=REASON)
async def test_runtime_worker(
    client: RuntimeClient, local_runtime_launcher, mock_myqos_creds
) -> None:
    bar = local_runtime_launcher(
        grpc_port=9090,
        myqos_worker="http://" + client.socket_address(),
        # make sure it has to talk to the other server for the test worker functions
        workers=[],
    )
    async with bar as runtime_server:
        await test_nint_adder(runtime_server)


@pytest.mark.asyncio
async def test_callback(client: RuntimeClient):
    tg = TierkreisGraph()
    idnode = tg.add_func("python_nodes/id_with_callback", value=2)
    tg.set_outputs(out=idnode)

    assert (await client.run_graph(tg, {}))["out"].try_autopython() == 2


_foo_func = TierkreisFunction(
    "foo",
    TypeScheme(
        {"a": StarKind()},
        [],
        GraphType(
            inputs=Row({"value": VarType("a")}, None),
            outputs=Row({"res": PairType(VarType("a"), IntType())}, None),
        ),
    ),
    "no docs",
    ["value"],
    ["res"],
)


def test_infer_graph_types():
    tg = TierkreisGraph()
    foo = tg.add_func("foo", value=3)
    tg.set_outputs(out=foo["res"])
    with pytest.raises(TierkreisTypeErrors, match="unknown function 'foo'"):
        infer_graph_types(tg, [])
    tg = infer_graph_types(tg, [_foo_func])
    out_type = tg.get_edge(NodePort(foo, "res"), NodePort(tg.output, "out")).type_
    assert out_type == PairType(IntType(), IntType())


@pytest.mark.asyncio
async def test_infer_graph_types_with_sig(client: RuntimeClient):
    # client is only used for signatures of builtins etc.
    sigs = await client.get_signature()

    tg = TierkreisGraph()
    mkp = tg.add_func(
        sigs["builtin"].functions["make_pair"], first=tg.input["in"], second=3
    )
    tg.set_outputs(val=mkp["pair"])

    tg = infer_graph_types(tg, sigs)
    in_type = tg.get_edge(NodePort(tg.input, "in"), NodePort(mkp, "first")).type_
    assert isinstance(in_type, VarType)
    out_type = tg.get_edge(NodePort(mkp, "pair"), NodePort(tg.output, "val")).type_
    assert out_type == PairType(in_type, IntType())


@pytest.mark.asyncio
async def test_infer_graph_types_with_inputs(client: RuntimeClient):
    funcs = [
        (await client.get_signature())["python_nodes"].functions["id_py"],
        _foo_func,
    ]
    tg = TierkreisGraph()
    foo = tg.add_func("foo", value=tg.input["inp"])
    tg.set_outputs(out=foo["res"])

    tg2 = deepcopy(tg)

    inputs = StructValue({"inp": FloatValue(3.14)})
    tg, inputs_ = infer_graph_types(tg, funcs, inputs)
    assert inputs_ == inputs
    out_type = tg.get_edge(NodePort(foo, "res"), NodePort(tg.output, "out")).type_
    assert out_type == PairType(FloatType(), IntType())

    graph_inputs = StructValue({"inp": GraphValue(idpy_graph())})
    with pytest.raises(TierkreisTypeErrors):
        # Pass an argument inconsistent with the annotations now on tg
        infer_graph_types(tg, funcs, graph_inputs)

    # deep copy (above) has no annotations yet, so ok
    tg2, inputs_ = infer_graph_types(tg2, funcs, graph_inputs)
    out_type = tg2.get_edge(NodePort(foo, "res"), NodePort(tg2.output, "out")).type_
    assert isinstance(out_type, PairType) and out_type.second == IntType()
    assert isinstance(out_type.first, GraphType)
    argtypes, restypes = out_type.first.inputs, out_type.first.outputs
    assert argtypes.rest is restypes.rest is None
    assert len(argtypes.content) == len(restypes.content) == 1
    assert argtypes.content["id_in"] == restypes.content["id_out"]
    assert isinstance(argtypes.content["id_in"], VarType)
