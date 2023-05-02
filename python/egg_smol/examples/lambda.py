"""
Lambda Calculus
===============
"""
# mypy: disable-error-code=empty-body
from __future__ import annotations

from typing import Callable, ClassVar

from egg_smol import *

egraph = EGraph()

# TODO: Debug extracting constants


@egraph.class_
class Val(BaseExpr):
    """
    A value is a number or a boolean.
    """

    TRUE: ClassVar[Val]
    FALSE: ClassVar[Val]

    def __init__(self, v: i64Like) -> None:
        ...


@egraph.class_
class Var(BaseExpr):
    def __init__(self, v: StringLike) -> None:
        ...


@egraph.class_
class Term(BaseExpr):
    @classmethod
    def val(cls, v: Val) -> Term:
        ...

    @classmethod
    def var(cls, v: Var) -> Term:
        ...

    def __add__(self, other: Term) -> Term:
        ...

    def __eq__(self, other: Term) -> Term:  # type: ignore[override]
        ...

    def __call__(self, other: Term) -> Term:
        ...

    def eval(self) -> Val:
        ...

    def v(self) -> Var:
        ...


@egraph.function
def lam(x: Var, t: Term) -> Term:
    ...


@egraph.function
def let_(x: Var, t: Term, b: Term) -> Term:
    ...


@egraph.function
def fix(x: Var, t: Term) -> Term:
    ...


@egraph.function
def if_(c: Term, t: Term, f: Term) -> Term:
    ...


StringSet = Map[Var, i64]


@egraph.function(merge=lambda old, new: old & new)
def freer(t: Term) -> StringSet:
    ...


(v, v1, v2) = vars_("v v1 v2", Val)
(t, t1, t2, t3, t4) = vars_("t t1 t2 t3 t4", Term)
(x, y) = vars_("x y", Var)
fv, fv1, fv2, fv3 = vars_("fv fv1 fv2 fv3", StringSet)
i1, i2 = vars_("i1 i2", i64)
egraph.register(
    # freer
    rule(eq(t).to(Term.val(v))).then(set_(freer(t)).to(StringSet.empty())),
    rule(eq(t).to(Term.var(x))).then(set_(freer(t)).to(StringSet.empty().insert(x, i64(1)))),
    rule(eq(t).to(t1 + t2), eq(freer(t1)).to(fv1), eq(freer(t2)).to(fv2)).then(set_(freer(t)).to(fv1 | fv2)),
    rule(eq(t).to(t1 == t2), eq(freer(t1)).to(fv1), eq(freer(t2)).to(fv2)).then(set_(freer(t)).to(fv1 | fv2)),
    rule(eq(t).to(t1(t2)), eq(freer(t1)).to(fv1), eq(freer(t2)).to(fv2)).then(set_(freer(t)).to(fv1 | fv2)),
    rule(eq(t).to(lam(x, t1)), eq(freer(t1)).to(fv)).then(set_(freer(t)).to(fv.remove(x))),
    rule(eq(t).to(let_(x, t1, t2)), eq(freer(t1)).to(fv1), eq(freer(t2)).to(fv2)).then(
        set_(freer(t)).to(fv1.remove(x) | fv2)
    ),
    rule(eq(t).to(fix(x, t1)), eq(freer(t1)).to(fv)).then(set_(freer(t)).to(fv.remove(x))),
    rule(eq(t).to(if_(t1, t2, t3)), eq(freer(t1)).to(fv1), eq(freer(t2)).to(fv2), eq(freer(t3)).to(fv3)).then(
        set_(freer(t)).to(fv1 | fv2 | fv3)
    ),
    # eval
    rule(eq(t).to(Term.val(v))).then(set_(t.eval()).to(v)),
    rule(eq(t).to(t1 + t2), eq(Val(i1)).to(t1.eval()), eq(Val(i2)).to(t2.eval())).then(
        union(t.eval()).with_(Val(i1 + i2))
    ),
    rule(eq(t).to(t1 == t2), eq(t1.eval()).to(t2.eval())).then(union(t.eval()).with_(Val.TRUE)),
    rule(eq(t).to(t1 == t2), eq(t1.eval()).to(v1), eq(t2.eval()).to(v2), v1 != v2).then(
        union(t.eval()).with_(Val.FALSE)
    ),
    rule(eq(v).to(t.eval())).then(union(t).with_(Term.val(v))),
    # if
    rewrite(if_(Term.val(Val.TRUE), t1, t2)).to(t1),
    rewrite(if_(Term.val(Val.FALSE), t1, t2)).to(t2),
    # if-elim
    # Adds let rules so next one can match on them
    rule(eq(t).to(if_(Term.var(x) == t1, t2, t3))).then(let_(x, t1, t2), let_(x, t1, t3)),
    rewrite(if_(Term.var(x) == t1, t2, t3)).to(
        t3,
        eq(let_(x, t1, t2)).to(let_(x, t1, t3)),
    ),
    # add-comm
    rewrite(t1 + t2).to(t2 + t1),
    # add-assoc
    rewrite((t1 + t2) + t3).to(t1 + (t2 + t3)),
    # eq-comm
    rewrite(t1 == t2).to(t2 == t1),
    # Fix
    rewrite(fix(x, t)).to(let_(x, fix(x, t), t)),
    # beta reduction
    rewrite(lam(x, t)(t1)).to(let_(x, t1, t)),
    # let-app
    rewrite(let_(x, t, t1(t2))).to(let_(x, t, t1)(let_(x, t, t2))),
    # let-add
    rewrite(let_(x, t, t1 + t2)).to(let_(x, t, t1) + let_(x, t, t2)),
    # let-eq
    rewrite(let_(x, t, t1 == t2)).to(let_(x, t, t1) == let_(x, t, t2)),
    # let-const
    rewrite(let_(x, t, Term.val(v))).to(Term.val(v)),
    # let-if
    rewrite(let_(x, t, if_(t1, t2, t3))).to(if_(let_(x, t, t1), let_(x, t, t2), let_(x, t, t3))),
    # let-var-same
    rewrite(let_(x, t, Term.var(x))).to(t),
    # let-var-diff
    rewrite(let_(x, t, Term.var(y))).to(Term.var(y), x != y),
    # let-lam-same
    rewrite(let_(x, t, lam(x, t1))).to(lam(x, t1)),
    # let-lam-diff
    rewrite(let_(x, t, lam(y, t1))).to(lam(y, let_(x, t, t1)), x != y, eq(fv).to(freer(t)), fv.not_contains(y)),
    rule(eq(t).to(let_(x, t1, lam(y, t2))), x != y, eq(fv).to(freer(t1)), fv.contains(y)).then(
        union(t).with_(lam(t.v(), let_(x, t1, let_(y, Term.var(t.v()), t2))))
    ),
)


def assert_simplifies(l: BaseExpr, r: BaseExpr) -> None:
    """
    Simplify and print
    """
    res = egraph.simplify(l, 20)
    print(f"{l} ➡  {res}")
    assert expr_parts(res) == expr_parts(r), f"{res} != {r}"


assert_simplifies((Term.val(Val(1))).eval(), Val(1))
assert_simplifies((Term.val(Val(1)) + Term.val(Val(2))).eval(), Val(3))


result = egraph.relation("result")


def l(fn: Callable[[Term], Term]) -> Term:
    """
    Create a lambda term from a function
    """
    # Use first var name from fn
    x = fn.__code__.co_varnames[0]
    return lam(Var(x), fn(Term.var(Var(x))))


# lambda under
assert_simplifies(
    l(lambda x: Term.val(Val(4)) + l(lambda y: y)(Term.val(Val(4)))),
    l(lambda x: Term.val(Val(8))),
)
