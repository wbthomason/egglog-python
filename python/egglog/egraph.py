from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import InitVar, dataclass, field
from inspect import Parameter, currentframe, signature
from types import FunctionType
from typing import _GenericAlias  # type: ignore[attr-defined]
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    Iterable,
    Literal,
    NoReturn,
    Optional,
    TypeVar,
    Union,
    cast,
    get_type_hints,
    overload,
)

import graphviz
from egglog.declarations import Declarations
from typing_extensions import ParamSpec, get_args, get_origin

from . import bindings
from .declarations import *
from .monkeypatch import monkeypatch_forward_ref
from .runtime import *
from .runtime import _resolve_callable, class_to_ref

if TYPE_CHECKING:
    from .builtins import String

monkeypatch_forward_ref()

__all__ = [
    "EGraph",
    "Module",
    "BUILTINS",
    "BaseExpr",
    "Unit",
    "rewrite",
    "eq",
    "panic",
    "let",
    "delete",
    "union",
    "set_",
    "rule",
    "var",
    "vars_",
    "Fact",
    "expr_parts",
    "Schedule",
    "run",
    "seq",
]

T = TypeVar("T")
P = ParamSpec("P")
TYPE = TypeVar("TYPE", bound="type[BaseExpr]")
CALLABLE = TypeVar("CALLABLE", bound=Callable)
EXPR = TypeVar("EXPR", bound="BaseExpr")
E1 = TypeVar("E1", bound="BaseExpr")
E2 = TypeVar("E2", bound="BaseExpr")
E3 = TypeVar("E3", bound="BaseExpr")
E4 = TypeVar("E4", bound="BaseExpr")
# Attributes which are sometimes added to classes by the interpreter or the dataclass decorator, or by ipython.
# We ignore these when inspecting the class.

IGNORED_ATTRIBUTES = {
    "__module__",
    "__doc__",
    "__dict__",
    "__weakref__",
    "__orig_bases__",
    "__annotations__",
    "__hash__",
}


_BUILTIN_DECLS: Declarations | None = None


@dataclass
class _BaseModule(ABC):
    """
    Base Module which provides methods to register sorts, expressions, actions etc.

    Inherited by:
    - EGraph: Holds a live EGraph instance
    - Builtins: Stores a list of the builtins which have already been pre-regsietered
    - Module: Stores a list of commands and additional declerations
    """

    # Any modules you want to depend on
    deps: InitVar[list[Module]] = []
    # All dependencies flattened
    _flatted_deps: list[Module] = field(init=False, default_factory=list)
    _mod_decls: ModuleDeclarations = field(init=False)

    def __post_init__(self, modules: list[Module] = []) -> None:
        included_decls = [_BUILTIN_DECLS] if _BUILTIN_DECLS else []
        # Traverse all the included modules to flatten all their dependencies and add to the included declerations
        for mod in modules:
            for child_mod in [*mod._flatted_deps, mod]:
                if child_mod not in self._flatted_deps:
                    self._flatted_deps.append(child_mod)
                    included_decls.append(child_mod._mod_decls._decl)
        self._mod_decls = ModuleDeclarations(Declarations(), included_decls)

    @abstractmethod
    def _process_commands(self, cmds: Iterable[bindings._Command]) -> None:
        """
        Process the commands generated by this module.
        """
        raise NotImplementedError

    @overload
    def class_(self, *, egg_sort: str) -> Callable[[TYPE], TYPE]:
        ...

    @overload
    def class_(self, cls: TYPE, /) -> TYPE:
        ...

    def class_(self, *args, **kwargs) -> Any:
        """
        Registers a class.
        """
        frame = currentframe()
        assert frame
        prev_frame = frame.f_back
        assert prev_frame

        if kwargs:
            assert set(kwargs.keys()) == {"egg_sort"}
            return lambda cls: self._class(cls, prev_frame.f_locals, prev_frame.f_globals, kwargs["egg_sort"])
        assert len(args) == 1
        return self._class(args[0], prev_frame.f_locals, prev_frame.f_globals)

    def _class(
        self,
        cls: type[BaseExpr],
        hint_locals: dict[str, Any],
        hint_globals: dict[str, Any],
        egg_sort: Optional[str] = None,
    ) -> RuntimeClass:
        """
        Registers a class.
        """
        cls_name = cls.__name__
        # Get all the methods from the class
        cls_dict: dict[str, Any] = {k: v for k, v in cls.__dict__.items() if k not in IGNORED_ATTRIBUTES}
        parameters: list[TypeVar] = cls_dict.pop("__parameters__", [])

        n_type_vars = len(parameters)
        self._process_commands(self._mod_decls.register_class(cls_name, n_type_vars, egg_sort))
        # The type ref of self is paramterized by the type vars
        slf_type_ref = TypeRefWithVars(cls_name, tuple(ClassTypeVarRef(i) for i in range(n_type_vars)))

        # First register any class vars as constants
        hint_globals = hint_globals.copy()
        hint_globals[cls_name] = cls
        for k, v in get_type_hints(cls, globalns=hint_globals, localns=hint_locals).items():
            if v.__origin__ == ClassVar:
                (inner_tp,) = v.__args__
                self._register_constant(ClassVariableRef(cls_name, k), inner_tp, None, (cls, cls_name))
            else:
                raise NotImplementedError("The only supported annotations on class attributes are class vars")

        # Then register each of its methods
        for method_name, method in cls_dict.items():
            is_init = method_name == "__init__"
            # Don't register the init methods for literals, since those don't use the type checking mechanisms
            if is_init and cls_name in LIT_CLASS_NAMES:
                continue
            if isinstance(method, _WrappedMethod):
                fn = method.fn
                egg_fn = method.egg_fn
                cost = method.cost
                default = method.default
                merge = method.merge
                on_merge = method.on_merge
            else:
                fn = method
                egg_fn, cost, default, merge, on_merge = None, None, None, None, None
            if isinstance(fn, classmethod):
                fn = fn.__func__
                is_classmethod = True
            else:
                # We count __init__ as a classmethod since it is called on the class
                is_classmethod = is_init

            ref: ClassMethodRef | MethodRef = (
                ClassMethodRef(cls_name, method_name) if is_classmethod else MethodRef(cls_name, method_name)
            )
            self._register_function(
                ref,
                egg_fn,
                fn,
                hint_locals,
                default,
                cost,
                merge,
                on_merge,
                "cls" if is_classmethod and not is_init else slf_type_ref,
                parameters,
                is_init,
                # If this is an i64, use the runtime class for the alias so that i64Like is resolved properly
                # Otherwise, this might be a Map in which case pass in the original cls so that we
                # can do Map[T, V] on it, which is not allowed on the runtime class
                cls_type_and_name=(
                    RuntimeClass(self._mod_decls, cls_name) if cls_name in {"i64", "String"} else cls,
                    cls_name,
                ),
            )

        # Register != as a method so we can print it as a string
        self._mod_decls._decl.register_callable_ref(MethodRef(cls_name, "__ne__"), "!=")
        return RuntimeClass(self._mod_decls, cls_name)

    # We seperate the function and method overloads to make it simpler to know if we are modifying a function or method,
    # So that we can add the functions eagerly to the registry and wait on the methods till we process the class.

    # We have to seperate method/function overloads for those that use the T params and those that don't
    # Otherwise, if you say just pass in `cost` then the T param is inferred as `Nothing` and
    # It will break the typing.
    @overload
    def method(  # type: ignore
        self,
        *,
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        merge: Optional[Callable[[Any, Any], Any]] = None,
        on_merge: Optional[Callable[[Any, Any], Iterable[ActionLike]]] = None,
    ) -> Callable[[CALLABLE], CALLABLE]:
        ...

    @overload
    def method(
        self,
        *,
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        default: Optional[EXPR] = None,
        merge: Optional[Callable[[EXPR, EXPR], EXPR]] = None,
        on_merge: Optional[Callable[[EXPR, EXPR], Iterable[ActionLike]]] = None,
    ) -> Callable[[Callable[P, EXPR]], Callable[P, EXPR]]:
        ...

    def method(
        self,
        *,
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        default: Optional[EXPR] = None,
        merge: Optional[Callable[[EXPR, EXPR], EXPR]] = None,
        on_merge: Optional[Callable[[EXPR, EXPR], Iterable[ActionLike]]] = None,
    ) -> Callable[[Callable[P, EXPR]], Callable[P, EXPR]]:
        return lambda fn: _WrappedMethod(egg_fn, cost, default, merge, on_merge, fn)

    @overload
    def function(self, fn: CALLABLE, /) -> CALLABLE:
        ...

    @overload
    def function(  # type: ignore
        self,
        *,
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        merge: Optional[Callable[[Any, Any], Any]] = None,
        on_merge: Optional[Callable[[Any, Any], Iterable[ActionLike]]] = None,
    ) -> Callable[[CALLABLE], CALLABLE]:
        ...

    @overload
    def function(
        self,
        *,
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        default: Optional[EXPR] = None,
        merge: Optional[Callable[[EXPR, EXPR], EXPR]] = None,
        on_merge: Optional[Callable[[EXPR, EXPR], Iterable[ActionLike]]] = None,
    ) -> Callable[[Callable[P, EXPR]], Callable[P, EXPR]]:
        ...

    def function(self, *args, **kwargs) -> Any:
        """
        Registers a function.
        """
        fn_locals = currentframe().f_back.f_locals  # type: ignore

        # If we have any positional args, then we are calling it directly on a function
        if args:
            assert len(args) == 1
            return self._function(args[0], fn_locals)
        # otherwise, we are passing some keyword args, so save those, and then return a partial
        return lambda fn: self._function(fn, fn_locals, **kwargs)

    def _function(
        self,
        fn: Callable[..., RuntimeExpr],
        hint_locals: dict[str, Any],
        egg_fn: Optional[str] = None,
        cost: Optional[int] = None,
        default: Optional[RuntimeExpr] = None,
        merge: Optional[Callable[[RuntimeExpr, RuntimeExpr], RuntimeExpr]] = None,
        on_merge: Optional[Callable[[RuntimeExpr, RuntimeExpr], Iterable[ActionLike]]] = None,
    ) -> RuntimeFunction:
        """
        Uncurried version of function decorator
        """
        name = fn.__name__
        # Save function decleartion
        self._register_function(FunctionRef(name), egg_fn, fn, hint_locals, default, cost, merge, on_merge)
        # Return a runtime function which will act like the decleration
        return RuntimeFunction(self._mod_decls, name)

    def _register_function(
        self,
        ref: FunctionCallableRef,
        egg_name: Optional[str],
        fn: Any,
        # Pass in the locals, retrieved from the frame when wrapping,
        # so that we support classes and function defined inside of other functions (which won't show up in the globals)
        hint_locals: dict[str, Any],
        default: Optional[RuntimeExpr],
        cost: Optional[int],
        merge: Optional[Callable[[RuntimeExpr, RuntimeExpr], RuntimeExpr]],
        on_merge: Optional[Callable[[RuntimeExpr, RuntimeExpr], Iterable[ActionLike]]],
        # The first arg is either cls, for a classmethod, a self type, or none for a function
        first_arg: Literal["cls"] | TypeOrVarRef | None = None,
        cls_typevars: list[TypeVar] = [],
        is_init: bool = False,
        cls_type_and_name: Optional[tuple[type | RuntimeClass, str]] = None,
    ) -> None:
        if not isinstance(fn, FunctionType):
            raise NotImplementedError(f"Can only generate function decls for functions not {fn}  {type(fn)}")

        hint_globals = fn.__globals__.copy()

        if cls_type_and_name:
            hint_globals[cls_type_and_name[1]] = cls_type_and_name[0]
        hints = get_type_hints(fn, hint_globals, hint_locals)
        # If this is an init fn use the first arg as the return type
        if is_init:
            if not isinstance(first_arg, (ClassTypeVarRef, TypeRefWithVars)):
                raise ValueError("Init function must have a self type")
            return_type = first_arg
        else:
            return_type = self._resolve_type_annotation(hints["return"], cls_typevars, cls_type_and_name)

        params = list(signature(fn).parameters.values())
        # Remove first arg if this is a classmethod or a method, since it won't have an annotation
        if first_arg is not None:
            first, *params = params
            if first.annotation != Parameter.empty:
                raise ValueError(f"First arg of a method must not have an annotation, not {first.annotation}")

        # Check that all the params are positional or keyword, and that there is only one var arg at the end
        found_var_arg = False
        for param in params:
            if found_var_arg:
                raise ValueError("Can only have a single var arg at the end")
            kind = param.kind
            if kind == Parameter.VAR_POSITIONAL:
                found_var_arg = True
            elif kind != Parameter.POSITIONAL_OR_KEYWORD:
                raise ValueError(f"Can only register functions with positional or keyword args, not {param.kind}")

        if found_var_arg:
            var_arg_param, *params = params
            var_arg_type = self._resolve_type_annotation(hints[var_arg_param.name], cls_typevars, cls_type_and_name)
        else:
            var_arg_type = None
        arg_types = tuple(self._resolve_type_annotation(hints[t.name], cls_typevars, cls_type_and_name) for t in params)
        # If the first arg is a self, and this not an __init__ fn, add this as a typeref
        if isinstance(first_arg, (ClassTypeVarRef, TypeRefWithVars)) and not is_init:
            arg_types = (first_arg,) + arg_types

        default_decl = None if default is None else default.__egg_typed_expr__.expr
        merge_decl = (
            None
            if merge is None
            else merge(
                RuntimeExpr(self._mod_decls, TypedExprDecl(return_type.to_just(), VarDecl("old"))),
                RuntimeExpr(self._mod_decls, TypedExprDecl(return_type.to_just(), VarDecl("new"))),
            ).__egg_typed_expr__.expr
        )
        merge_action = (
            []
            if on_merge is None
            else _action_likes(
                on_merge(
                    RuntimeExpr(self._mod_decls, TypedExprDecl(return_type.to_just(), VarDecl("old"))),
                    RuntimeExpr(self._mod_decls, TypedExprDecl(return_type.to_just(), VarDecl("new"))),
                )
            )
        )
        fn_decl = FunctionDecl(return_type=return_type, var_arg_type=var_arg_type, arg_types=arg_types)
        self._process_commands(
            self._mod_decls.register_function_callable(
                ref, fn_decl, egg_name, cost, default_decl, merge_decl, merge_action
            )
        )

    def _resolve_type_annotation(
        self,
        tp: object,
        cls_typevars: list[TypeVar],
        cls_type_and_name: Optional[tuple[type | RuntimeClass, str]],
    ) -> TypeOrVarRef:
        if isinstance(tp, TypeVar):
            return ClassTypeVarRef(cls_typevars.index(tp))
        # If there is a union, it should be of a literal and another type to allow type promotion
        if get_origin(tp) == Union:
            args = get_args(tp)
            if len(args) != 2:
                raise TypeError("Union types are only supported for type promotion")
            fst, snd = args
            if fst in {int, str, float}:
                return self._resolve_type_annotation(snd, cls_typevars, cls_type_and_name)
            if snd in {int, str, float}:
                return self._resolve_type_annotation(fst, cls_typevars, cls_type_and_name)
            raise TypeError("Union types are only supported for type promotion")

        # If this is the type for the class, use the class name
        if cls_type_and_name and tp == cls_type_and_name[0]:
            return TypeRefWithVars(cls_type_and_name[1])

        # If this is the class for this method and we have a paramaterized class, recurse
        if (
            cls_type_and_name
            and isinstance(tp, _GenericAlias)
            and tp.__origin__ == cls_type_and_name[0]  # type: ignore
        ):
            return TypeRefWithVars(
                cls_type_and_name[1],
                tuple(
                    self._resolve_type_annotation(a, cls_typevars, cls_type_and_name)
                    for a in tp.__args__  # type: ignore
                ),
            )

        if isinstance(tp, (RuntimeClass, RuntimeParamaterizedClass)):
            return class_to_ref(tp).to_var()
        raise TypeError(f"Unexpected type annotation {tp}")

    def register(self, command_or_generator: CommandLike | CommandGenerator, *commands: CommandLike) -> None:
        """
        Registers any number of rewrites or rules.
        """
        if isinstance(command_or_generator, FunctionType):
            assert not commands
            commands = tuple(_command_generator(command_or_generator))
        else:
            commands = (cast(CommandLike, command_or_generator), *commands)
        self._process_commands(_command_like(command)._to_egg_command(self._mod_decls) for command in commands)

    def ruleset(self, name: str) -> Ruleset:
        self._process_commands([bindings.AddRuleset(name)])
        return Ruleset(name)

    # Overload to support aritys 0-4 until variadic generic support map, so we can map from type to value
    @overload
    def relation(
        self, name: str, tp1: type[E1], tp2: type[E2], tp3: type[E3], tp4: type[E4], /
    ) -> Callable[[E1, E2, E3, E4], Unit]:
        ...

    @overload
    def relation(self, name: str, tp1: type[E1], tp2: type[E2], tp3: type[E3], /) -> Callable[[E1, E2, E3], Unit]:
        ...

    @overload
    def relation(self, name: str, tp1: type[E1], tp2: type[E2], /) -> Callable[[E1, E2], Unit]:
        ...

    @overload
    def relation(self, name: str, tp1: type[T], /, *, egg_fn: Optional[str] = None) -> Callable[[T], Unit]:
        ...

    @overload
    def relation(self, name: str, /, *, egg_fn: Optional[str] = None) -> Callable[[], Unit]:
        ...

    def relation(self, name: str, /, *tps: type, egg_fn: Optional[str] = None) -> Callable[..., Unit]:
        """
        Defines a relation, which is the same as a function which returns unit.
        """
        arg_types = tuple(self._resolve_type_annotation(cast(object, tp), [], None) for tp in tps)
        fn_decl = FunctionDecl(arg_types, TypeRefWithVars("Unit"))
        commands = self._mod_decls.register_function_callable(
            FunctionRef(name), fn_decl, egg_fn, cost=None, default=None, merge=None, merge_action=[]
        )
        self._process_commands(commands)
        return cast(Callable[..., Unit], RuntimeFunction(self._mod_decls, name))

    def input(self, fn: Callable[..., String], path: str) -> None:
        """
        Loads a CSV file and sets it as *input, output of the function.
        """
        fn_name = self._mod_decls.get_egg_fn(_resolve_callable(fn))
        self._process_commands([bindings.Input(fn_name, path)])

    def constant(self, name: str, tp: type[EXPR], egg_name: Optional[str] = None) -> EXPR:
        """
        Defines a named constant of a certain type.

        This is the same as defining a nullary function with a high cost.
        """
        ref = ConstantRef(name)
        type_ref = self._register_constant(ref, tp, egg_name, None)
        return cast(EXPR, RuntimeExpr(self._mod_decls, TypedExprDecl(type_ref, CallDecl(ref))))

    def _register_constant(
        self,
        ref: ConstantRef | ClassVariableRef,
        tp: object,
        egg_name: Optional[str],
        cls_type_and_name: Optional[tuple[type | RuntimeClass, str]],
    ) -> JustTypeRef:
        """
        Register a constant, returning its typeref().
        """
        type_ref = self._resolve_type_annotation(tp, [], cls_type_and_name).to_just()
        self._process_commands(self._mod_decls.register_constant_callable(ref, type_ref, egg_name))
        return type_ref

    def define(self, name: str, expr: EXPR) -> EXPR:
        """
        Define a new expression in the egraph and return a reference to it.
        """
        # Don't support cost and maybe will be removed in favor of let
        # https://github.com/egraphs-good/egglog/issues/128#issuecomment-1523760578
        typed_expr = expr_parts(expr)
        self._process_commands([bindings.Define(name, typed_expr.to_egg(self._mod_decls), None)])
        return cast(EXPR, RuntimeExpr(self._mod_decls, TypedExprDecl(typed_expr.tp, VarDecl(name))))


@dataclass
class _Builtins(_BaseModule):
    def __post_init__(self, modules: list[Module] = []) -> None:
        """
        Register these declarations as builtins, so others can use them.
        """
        assert not modules
        super().__post_init__(modules)
        global _BUILTIN_DECLS
        if _BUILTIN_DECLS is not None:
            raise RuntimeError("Builtins already initialized")
        _BUILTIN_DECLS = self._mod_decls._decl

    def _process_commands(self, cmds: Iterable[bindings._Command]) -> None:
        """
        Commands which would have been used to create the builtins are discarded, since they are already registered.
        """
        pass


@dataclass
class Module(_BaseModule):
    _cmds: list[bindings._Command] = field(default_factory=list, repr=False)

    def _process_commands(self, cmds: Iterable[bindings._Command]) -> None:
        self._cmds.extend(cmds)


@dataclass
class EGraph(_BaseModule):
    """
    Represents an EGraph instance at runtime
    """

    _egraph: bindings.EGraph = field(repr=False, default_factory=bindings.EGraph)
    # The current declarations which have been pushed to the stack
    _decl_stack: list[Declarations] = field(default_factory=list, repr=False)

    def __post_init__(self, modules: list[Module] = []) -> None:
        super().__post_init__(modules)
        for m in self._flatted_deps:
            self._process_commands(m._cmds)

    def _process_commands(self, commands: Iterable[bindings._Command]) -> None:
        self._egraph.run_program(*commands)

    def _repr_mimebundle_(self, *args, **kwargs):
        """
        Returns the graphviz representation of the e-graph.
        """

        return self.graphviz._repr_mimebundle_(*args, **kwargs)

    @property
    def graphviz(self) -> graphviz.Source:
        return graphviz.Source(self._egraph.to_graphviz_string())

    def _repr_html_(self) -> str:
        """
        Add a _repr_html_ to be an SVG to work with sphinx gallery
        ala https://github.com/xflr6/graphviz/pull/121
        until this PR is merged and released
        https://github.com/sphinx-gallery/sphinx-gallery/pull/1138
        """
        return self.graphviz.pipe(format="svg").decode()

    def display(self):
        """
        Displays the e-graph in the notebook.
        """
        from IPython.display import display

        display(self)

    def simplify(self, expr: EXPR, limit: int, *until: Fact, ruleset: Optional[Ruleset] = None) -> EXPR:
        """
        Simplifies the given expression.
        """
        typed_expr = expr_parts(expr)
        egg_expr = typed_expr.to_egg(self._mod_decls)
        self._process_commands(
            [bindings.Simplify(egg_expr, Run(limit, _ruleset_name(ruleset), until)._to_egg_config(self._mod_decls))]
        )
        extract_report = self._egraph.extract_report()
        if not extract_report:
            raise ValueError("No extract report saved")
        new_typed_expr = TypedExprDecl.from_egg(self._mod_decls, extract_report.expr)
        return cast(EXPR, RuntimeExpr(self._mod_decls, new_typed_expr))

    def include(self, path: str) -> None:
        """
        Include a file of rules.
        """
        raise NotImplementedError(
            "Not implemented yet, because we don't have a way of registering the types with Python"
        )

    def output(self) -> None:
        raise NotImplementedError("Not imeplemented yet, because there are no examples in the egglog repo")

    @overload
    def run(self, limit: int, /, *until: Fact, ruleset: Optional[Ruleset] = None) -> bindings.RunReport:
        ...

    @overload
    def run(self, schedule: Schedule, /) -> bindings.RunReport:
        ...

    def run(
        self, limit_or_schedule: int | Schedule, /, *until: Fact, ruleset: Optional[Ruleset] = None
    ) -> bindings.RunReport:
        """
        Run the egraph until the given limit or until the given facts are true.
        """
        if isinstance(limit_or_schedule, int):
            limit_or_schedule = run(ruleset, limit_or_schedule, *until)
        return self._run_schedule(limit_or_schedule)

    def _run_schedule(self, schedule: Schedule) -> bindings.RunReport:
        self._process_commands([bindings.RunScheduleCommand(schedule._to_egg_schedule(self._mod_decls))])
        run_report = self._egraph.run_report()
        if not run_report:
            raise ValueError("No run report saved")
        return run_report

    def check(self, *facts: FactLike) -> None:
        """
        Check if a fact is true in the egraph.
        """
        self._process_commands([self._facts_to_check(facts)])

    def check_fail(self, *facts: FactLike) -> None:
        """
        Checks that one of the facts is not true
        """
        self._process_commands([bindings.Fail(self._facts_to_check(facts))])

    def _facts_to_check(self, facts: Iterable[FactLike]) -> bindings.Check:
        egg_facts = [f._to_egg_fact(self._mod_decls) for f in _fact_likes(facts)]
        return bindings.Check(egg_facts)

    def extract(self, expr: EXPR) -> EXPR:
        """
        Extract the lowest cost expression from the egraph.
        """
        typed_expr = expr_parts(expr)
        egg_expr = typed_expr.to_egg(self._mod_decls)
        extract_report = self._run_extract(egg_expr, 0)
        new_typed_expr = TypedExprDecl.from_egg(self._mod_decls, extract_report.expr)
        if new_typed_expr.tp != typed_expr.tp:
            raise RuntimeError(f"Type mismatch: {new_typed_expr.tp} != {typed_expr.tp}")
        return cast(EXPR, RuntimeExpr(self._mod_decls, new_typed_expr))

    def extract_multiple(self, expr: EXPR, n: int) -> list[EXPR]:
        """
        Extract multiple expressions from the egraph.
        """
        typed_expr = expr_parts(expr)
        egg_expr = typed_expr.to_egg(self._mod_decls)
        extract_report = self._run_extract(egg_expr, n)
        new_exprs = [TypedExprDecl.from_egg(self._mod_decls, egg_expr) for egg_expr in extract_report.variants]
        return [cast(EXPR, RuntimeExpr(self._mod_decls, expr)) for expr in new_exprs]

    def _run_extract(self, expr: bindings._Expr, n: int) -> bindings.ExtractReport:
        self._process_commands([bindings.Extract(n, expr)])
        extract_report = self._egraph.extract_report()
        if not extract_report:
            raise ValueError("No extract report saved")
        return extract_report

    def push(self) -> None:
        """
        Push the current state of the egraph, so that it can be popped later and reverted back.
        """
        self._process_commands([bindings.Push(1)])
        self._decl_stack.append(self._mod_decls._decl)
        self._decls = deepcopy(self._mod_decls._decl)

    def pop(self) -> None:
        """
        Pop the current state of the egraph, reverting back to the previous state.
        """
        self._process_commands([bindings.Pop(1)])
        self._mod_decls._decl = self._decl_stack.pop()

    def __enter__(self):
        """
        Copy the egraph state, so that it can be reverted back to the original state at the end.
        """
        self.push()

    def __exit__(self, exc_type, exc, exc_tb):
        self.pop()


@dataclass(frozen=True)
class _WrappedMethod(Generic[P, EXPR]):
    """
    Used to wrap a method and store some extra options on it before processing it when processing the class.
    """

    egg_fn: Optional[str]
    cost: Optional[int]
    default: Optional[EXPR]
    merge: Optional[Callable[[EXPR, EXPR], EXPR]]
    on_merge: Optional[Callable[[EXPR, EXPR], Iterable[ActionLike]]]
    fn: Callable[P, EXPR]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> EXPR:
        raise NotImplementedError("We should never call a wrapped method. Did you forget to wrap the class?")


class _BaseExprMetaclass(type):
    """
    Metaclass of BaseExpr, used to override isistance checks, so that runtime expressions are instances
    of BaseExpr at runtime.
    """

    def __instancecheck__(self, instance: object) -> bool:
        return isinstance(instance, RuntimeExpr)


class BaseExpr(metaclass=_BaseExprMetaclass):
    """
    Expression base class, which adds suport for != to all expression types.
    """

    def __ne__(self: EXPR, other_expr: EXPR) -> Unit:  # type: ignore[override, empty-body]
        """
        Compare whether to expressions are not equal.

        :param self: The expression to compare.
        :param other_expr: The other expression to compare to, which must be of the same type.
        :meta public:
        """
        ...

    def __eq__(self, other: NoReturn) -> NoReturn:  # type: ignore[override, empty-body]
        """
        Equality is currently not supported. We only add this method so that
        if you try to use it MyPy will warn you.
        """
        ...


BUILTINS = _Builtins()


@BUILTINS.class_(egg_sort="Unit")
class Unit(BaseExpr):
    """
    The unit type. This is also used to reprsent if a value exists, if it is resolved or not.
    """

    def __init__(self) -> None:
        ...


@dataclass(frozen=True)
class Ruleset:
    name: str


def _ruleset_name(ruleset: Optional[Ruleset]) -> str:
    return ruleset.name if ruleset else ""


# We use these builders so that when creating these structures we can type check
# if the arguments are the same type of expression


def rewrite(lhs: EXPR, ruleset: Optional[Ruleset] = None) -> _RewriteBuilder[EXPR]:
    """Rewrite the given expression to a new expression."""
    return _RewriteBuilder(lhs, ruleset)


def birewrite(lhs: EXPR, ruleset: Optional[Ruleset] = None) -> _BirewriteBuilder[EXPR]:
    """Rewrite the given expression to a new expression and vice versa."""
    return _BirewriteBuilder(lhs, ruleset)


def eq(expr: EXPR) -> _EqBuilder[EXPR]:
    """Check if the given expression is equal to the given value."""
    return _EqBuilder(expr)


def panic(message: str) -> Action:
    """Raise an error with the given message."""
    return Panic(message)


def let(name: str, expr: BaseExpr) -> Action:
    """Create a let binding."""
    return Let(name, expr_parts(expr).expr)


def expr_action(expr: BaseExpr) -> Action:
    typed_expr = expr_parts(expr)
    return ExprAction(typed_expr.expr)


def delete(expr: BaseExpr) -> Action:
    """Create a delete expression."""
    decl = expr_parts(expr).expr
    if not isinstance(decl, CallDecl):
        raise ValueError(f"Can only delete calls not {decl}")
    return Delete(decl)


def expr_fact(expr: BaseExpr) -> Fact:
    return ExprFact(expr_parts(expr).expr)


def union(lhs: EXPR) -> _UnionBuilder[EXPR]:
    """Create a union of the given expression."""
    return _UnionBuilder(lhs=lhs)


def set_(lhs: EXPR) -> _SetBuilder[EXPR]:
    """Create a set of the given expression."""
    return _SetBuilder(lhs=lhs)


def rule(*facts: FactLike, ruleset: Optional[Ruleset] = None, name: Optional[str] = None) -> _RuleBuilder:
    """Create a rule with the given facts."""
    return _RuleBuilder(facts=_fact_likes(facts), name=name, ruleset=ruleset)


def var(name: str, bound: type[EXPR]) -> EXPR:
    """Create a new variable with the given name and type."""
    return cast(EXPR, _var(name, bound))


def _var(name: str, bound: Any) -> RuntimeExpr:
    """Create a new variable with the given name and type."""
    if not isinstance(bound, (RuntimeClass, RuntimeParamaterizedClass)):
        raise TypeError(f"Unexpected type {type(bound)}")
    return RuntimeExpr(bound.__egg_decls__, TypedExprDecl(class_to_ref(bound), VarDecl(name)))


def vars_(names: str, bound: type[EXPR]) -> Iterable[EXPR]:
    """Create variables with the given names and type."""
    for name in names.split(" "):
        yield var(name, bound)


@dataclass
class _RewriteBuilder(Generic[EXPR]):
    lhs: EXPR
    ruleset: Optional[Ruleset]

    def to(self, rhs: EXPR, *conditions: FactLike) -> Command:
        return Rewrite(
            _ruleset_name(self.ruleset),
            expr_parts(self.lhs).expr,
            expr_parts(rhs).expr,
            _fact_likes(conditions),
        )

    def __str__(self) -> str:
        return f"rewrite({self.lhs})"


@dataclass
class _BirewriteBuilder(Generic[EXPR]):
    lhs: EXPR
    ruleset: Optional[Ruleset]

    def to(self, rhs: EXPR, *conditions: FactLike) -> Command:
        return BiRewrite(
            _ruleset_name(self.ruleset),
            expr_parts(self.lhs).expr,
            expr_parts(rhs).expr,
            _fact_likes(conditions),
        )

    def __str__(self) -> str:
        return f"birewrite({self.lhs})"


@dataclass
class _EqBuilder(Generic[EXPR]):
    expr: EXPR

    def to(self, *exprs: EXPR) -> Fact:
        return Eq(tuple(expr_parts(e).expr for e in (self.expr, *exprs)))

    def __str__(self) -> str:
        return f"eq({self.expr})"


@dataclass
class _SetBuilder(Generic[EXPR]):
    lhs: BaseExpr

    def to(self, rhs: EXPR) -> Action:
        lhs = expr_parts(self.lhs).expr
        if not isinstance(lhs, CallDecl):
            raise ValueError(f"Can only create a call with a call for the lhs, got {lhs}")
        return Set(lhs, expr_parts(rhs).expr)

    def __str__(self) -> str:
        return f"set_({self.lhs})"


@dataclass
class _UnionBuilder(Generic[EXPR]):
    lhs: BaseExpr

    def with_(self, rhs: EXPR) -> Action:
        return Union_(expr_parts(self.lhs).expr, expr_parts(rhs).expr)

    def __str__(self) -> str:
        return f"union({self.lhs})"


@dataclass
class _RuleBuilder:
    facts: tuple[Fact, ...]
    name: Optional[str]
    ruleset: Optional[Ruleset]

    def then(self, *actions: ActionLike) -> Command:
        return Rule(_action_likes(actions), self.facts, self.name or "", _ruleset_name(self.ruleset))


def expr_parts(expr: BaseExpr) -> TypedExprDecl:
    """
    Returns the underlying type and decleration of the expression. Useful for testing structural equality or debugging.
    """
    assert isinstance(expr, RuntimeExpr)
    return expr.__egg_typed_expr__


def run(ruleset: Optional[Ruleset] = None, limit: int = 1, *until: Fact) -> Run:
    """
    Create a run configuration.
    """
    return Run(limit, _ruleset_name(ruleset), tuple(until))


def seq(*schedules: Schedule) -> Schedule:
    """
    Run a sequence of schedules.
    """
    return Sequence(tuple(schedules))


CommandLike = Union[Command, BaseExpr]


def _command_like(command_like: CommandLike) -> Command:
    if isinstance(command_like, BaseExpr):
        return expr_action(command_like)
    return command_like


CommandGenerator = Callable[..., Iterable[Command]]


def _command_generator(gen: CommandGenerator) -> Iterable[Command]:
    """
    Calls the function with variables of the type and name of the arguments.
    """
    hints = get_type_hints(gen)
    args = (_var(p.name, hints[p.name]) for p in signature(gen).parameters.values())
    return gen(*args)


ActionLike = Union[Action, BaseExpr]


def _action_likes(action_likes: Iterable[ActionLike]) -> tuple[Action, ...]:
    return tuple(map(_action_like, action_likes))


def _action_like(action_like: ActionLike) -> Action:
    if isinstance(action_like, BaseExpr):
        return expr_action(action_like)
    return action_like


FactLike = Union[Fact, Unit]


def _fact_likes(fact_likes: Iterable[FactLike]) -> tuple[Fact, ...]:
    return tuple(map(_fact_like, fact_likes))


def _fact_like(fact_like: FactLike) -> Fact:
    if isinstance(fact_like, BaseExpr):
        return expr_fact(fact_like)
    return fact_like
