from __future__ import annotations

import sys
import warnings
from typing import TYPE_CHECKING

import structlog

from pydiverse.pipedag.context import ConfigContext, RunContext
from pydiverse.pipedag.core import Result
from pydiverse.pipedag.engine.base import OrchestrationEngine
from pydiverse.pipedag.util import requires

if TYPE_CHECKING:
    from pydiverse.pipedag.core import Flow, Task

try:
    import dask
except ImportError as e:
    warnings.warn(str(e), ImportWarning)
    dask = None


@requires(dask, ImportError("DaskEngine requires 'dask' to be installed."))
class DaskEngine(OrchestrationEngine):
    """Execute flow in parallel on a single machine using dask"""

    def __init__(self, **dask_compute_kwargs):
        self.dask_compute_kwargs = dict(
            traverse=True,
            optimize_graph=False,
            scheduler="processes",
            # Scheduler kwargs
            num_workers=None,
            chunksize=1,
        )

        self.dask_compute_kwargs.update(dask_compute_kwargs)

    def run(self, flow: Flow, **run_kwargs):
        g = flow.explicit_graph
        run_context = RunContext.get()
        config_context = ConfigContext.get()

        results = {}
        exception = None

        def bind_run(t: Task):
            structlog_config = structlog.get_config()
            structlog_context = structlog.contextvars.get_contextvars()

            def run(parent_futures, **kwargs):
                _ = parent_futures

                # TODO: Don't just assume a logger factory...
                structlog_config["logger_factory"] = structlog.PrintLoggerFactory(
                    sys.stderr
                )
                structlog.configure(**structlog_config)

                with structlog.contextvars.bound_contextvars(**structlog_context):
                    return t.run(**kwargs)

            run.__name__ = t.name
            return dask.delayed(run, pure=False)

        for task in flow.tasks:
            results[task] = bind_run(task)(
                parent_futures=[
                    results[parent_task] for parent_task, _ in g.in_edges(task)
                ],
                inputs={
                    in_id: results[in_t] for in_id, in_t in task.input_tasks.items()
                },
                run_context=run_context,
                config_context=config_context,
            )

        try:
            results = dask.compute(results, **self.dask_compute_kwargs)[0]
        except Exception as e:
            if run_kwargs.get("fail_fast", False):
                raise e
            exception = e

        return Result(
            underlying=results,
            successful=(exception is None),
            config_context=config_context,
            task_values=results,
            exception=exception,
        )
