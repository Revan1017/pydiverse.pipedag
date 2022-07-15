import contextlib
import threading
from typing import Iterator

import prefect

import pdpipedag
from pdpipedag.core import materialise
from pdpipedag.errors import SchemaError


class Schema:

    def __init__(self, name: str):
        self.name = name
        self.working_name = f'{name}__pipedag'
        self.task = SchemaSwapTask(self)
        self.materialising_tasks = []

        # Variables that should be accessed via a lock
        self.__lock = threading.Lock()
        self.__did_swap = False

        # Make sure that schema exists on database
        # This also ensures that this schema name is unique
        pdpipedag.config.store.create_schema(self)

    def __repr__(self):
        return f"<Schema: {self.name}>"

    def __enter__(self):
        self.flow: prefect.Flow = prefect.context.flow

        # Store current schema in context
        self._enter_schema = prefect.context.get('pipedag_schema')
        prefect.context.pipedag_schema = self

        # Add schema task to flow
        self.flow.add_task(self.task)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # The MaterialisingTask wrapper already adds the schema as a
        # downstream dependency. The only thing left to do is adding
        # the upstream dependencies to the upstream swap schema tasks.

        upstream_edges = self.flow.all_upstream_edges()

        def get_upstream_schemas(task: prefect.Task) -> Iterator['Schema']:
            """ Perform DFS and get all upstream schema dependencies.

            :param task: The task for which to get the upstream schemas.
            :return: Yields all upstream schemas.
            """
            visited = set()
            stack = [task]

            while stack:
                top = stack.pop()
                if top in visited:
                    continue
                visited.add(top)

                if isinstance(top, materialise.MaterialisingTask):
                    if top.schema != self:
                        yield top.schema
                        continue

                for edge in upstream_edges[top]:
                    stack.append(edge.upstream_task)

        # For each task, add the appropriate upstream schema swap dependencies
        for task in self.materialising_tasks:
            upstream_schemas = list(get_upstream_schemas(task))
            task.upstream_schemas = upstream_schemas
            for dependency in upstream_schemas:
                task.set_upstream(dependency.task)

        # Restore context
        prefect.context.pipedag_schema = self._enter_schema

    def add_task(self, task: prefect.Task):
        task.set_downstream(self.task)
        self.materialising_tasks.append(task)

    @property
    def current_name(self) -> str:
        """ The name of the schema where the data currently lives.

        Before a swap this is the working schema name and after a swap it is
        the actual schema name.
        """

        if self.did_swap:
            return self.name
        else:
            return self.working_name

    @property
    def did_swap(self) -> bool:
        with self.__lock:
            return self.__did_swap

    @contextlib.contextmanager
    def perform_swap(self):
        with self.__lock:
            if self.__did_swap:
                raise SchemaError(f"Schema {self.name} has already been swapped.")

            try:
                yield self
            finally:
                self.__did_swap = True


class SchemaSwapTask(prefect.Task):

    def __init__(self, schema):
        super().__init__(name = f'SchemaSwapTask({schema.name})')
        self.schema = schema
        self.child = None

    def run(self):
        self.logger.info('Performing schema swap.')
        pdpipedag.config.store.swap_schema(self.schema)
