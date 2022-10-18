import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from grpclib.client import Channel
from pytket import Circuit
from sympy import symbols

# from tierkreis.core.graphviz import render_graph, tierkreis_to_graphviz
from tierkreis.frontend.builder import (
    Box,
    Break,
    Const,
    Continue,
    Copyable,
    Else,
    If,
    IfElse,
    Input,
    Namespace,
    Output,
    closure,
    graph,
    loop,
)
from tierkreis.frontend.runtime_client import RuntimeClient, ServerRuntime

# This avoids errors on every call to a decorated _GraphDef
# pylint: disable=no-value-for-parameter


def runtime_client_from_args(args: list[str]) -> Optional[RuntimeClient]:
    if len(args) == 0:
        from tierkreis.frontend.python_runtime import PyRuntime

        tests_dir = Path(__file__).parent
        print("Importing", tests_dir)
        sys.path.append(str(tests_dir))
        import sc22_worker.main  # type: ignore

        workers_dir = tests_dir.parent.parent / "workers"
        print("Importing 2", workers_dir)
        sys.path.append(str(workers_dir))
        import pytket_worker.main  # type: ignore

        return PyRuntime(
            [
                sc22_worker.main.namespace,
                pytket_worker.main.namespace,
            ]
        )
    elif len(args) == 1:
        # cl.set_callback(lambda x, y: print(x, y))
        host, port = args[0].split(":")
        c = Channel(host, int(port))
        return ServerRuntime(c)
    else:
        return None


async def run_test(cl: RuntimeClient):
    sig = await cl.get_signature()
    root = Namespace(sig)
    bi, pt, sc = (
        root["builtin"],
        root["pytket"],
        root["sc22"],
    )

    a, b = symbols("a b")
    ansatz = Circuit(2)
    ansatz.Rx(0.5 + a, 0).Rx(-0.5 + b, 1).CZ(0, 1).Rx(0.5 + b, 0).Rx(
        -0.5 + a, 1
    ).measure_all()

    @graph(sig=sig)
    def initial(run) -> Output:
        init_params = Copyable(Const([0.2, 0.2]))
        init_score = bi.eval(run, params=init_params)
        p = bi.make_pair(init_params, init_score)
        lst = bi.push(Const([]), p)
        return Output(lst)

    init_box = Box(initial())

    @graph()
    def load_circuit() -> Output:
        js_str = json.dumps(ansatz.to_dict())
        c = pt.load_circuit_json(Const(js_str))
        return Output(c)

    @graph()
    def zexp_to_parity(zexp) -> Output:
        y = bi.fsub(Const(1.0), zexp)
        return Output(bi.fdiv(y, Const(2.0)))

    @graph()
    def main() -> Output:
        circ = Box(load_circuit())()

        @closure()
        def run_circuit(params: Input) -> Output:
            syms = Const(["a", "b"])
            # substitute parameters in circuit with values a, b
            subs = pt.substitute_symbols(circ, syms, params)
            res = pt.execute(subs, Const(1000), Const("AerBackend"))
            return Output(Box(zexp_to_parity())(pt.z_expectation(res)))

        run_circuit.copyable()

        @loop()
        def loop_def(initial: Input[Any]) -> Output:
            recs = Copyable(initial)
            new_cand = Copyable(sc.new_params(recs))
            score = run_circuit(new_cand)
            pair = bi.make_pair(new_cand, score)
            recs = Copyable(bi.push(recs, pair))

            with IfElse(sc.converged(recs)) as lbody:
                with If():
                    Break(recs)
                with Else():
                    Continue(recs)
            return Output(lbody.nref)

        init_val = init_box(run_circuit.graph_src)
        return Output(loop_def(init_val))

    tg = main()
    # tg
    # display(tg)
    # render_graph(tg, "../../figs/main_graph", "pdf")

    return await cl.run_graph(tg)


async def main():
    cl = runtime_client_from_args(sys.argv[1:])
    if cl is None:
        print(f"Usage: {__file__} [<host>:<port>]")
        sys.exit(-1)
    else:
        res = run_test(cl)
        print(res)


if __name__ == "__main__":
    asyncio.run(main())