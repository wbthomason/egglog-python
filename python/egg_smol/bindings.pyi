from datetime import timedelta
from typing import Optional

from typing_extensions import final

from .bindings_py import Expr, Fact_, FunctionDecl, Rewrite

@final
class Variant:
    def __init__(
        self, name: str, types: list[str], cost: Optional[int] = None
    ) -> None: ...
    name: str
    types: list[str]
    cost: Optional[int]

@final
class EGraph:
    def parse_and_run_program(self, input: str) -> list[str]: ...
    def declare_constructor(self, variant: Variant, sort: str) -> None: ...
    def declare_sort(self, name: str) -> None: ...
    def declare_function(self, decl: FunctionDecl) -> None: ...
    def define(self, name: str, expr: Expr, cost: Optional[int] = None) -> None: ...
    def add_rewrite(self, rewrite: Rewrite) -> str: ...
    def run_rules(self, limit: int) -> tuple[timedelta, timedelta, timedelta]: ...
    def check_fact(self, fact: Fact_) -> None: ...

@final
class EggSmolError(Exception):
    context: str
