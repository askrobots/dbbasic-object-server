"""Formula and rollup fields: derived values, materialized on write.

The spreadsheet feature (plan/formula-rollup-spec.md): a `type: computed`
field may declare either

    {"name": "full_name", "type": "computed",
     "formula": "first_name + \" \" + last_name"}

    {"name": "debit_total_cents", "type": "computed",
     "rollup": {"collection": "fin_journal_lines", "fk_field": "journal_id",
                "op": "sum", "field": "debit_cents"}}

and object_records keeps the stored value current: formulas recompute on
every write of the record itself; rollups recompute on writes/deletes in the
source collection (see object_records._recompute_rollups_for_source). Values
are STORED in the TSV — so lists, tables, detail, search, 58 filters, sort,
realtime, and backups all see them with zero UI changes.

The formula language is deliberately tiny and is evaluated by a
recursive-descent interpreter in this module — never Python eval. Grammar:

    expr   := term (("+" | "-") term)*
    term   := factor (("*" | "/") factor)*
    factor := NUMBER | STRING | IDENT | "(" expr ")" | "-" factor

`+` is numeric addition when both operands parse as numbers, else string
concatenation. Failure posture: a broken formula (unknown token, division by
zero, non-numeric arithmetic) yields an EMPTY value and never fails the
caller's write — derived values are non-authoritative, the opposite of
pre-write hooks (a broken gate must fail closed; a broken caption must not
block a save).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

ROLLUP_OPS = ("sum", "count", "min", "max", "avg")


class FormulaError(ValueError):
    """Raised internally for any formula parse/eval problem."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_OPS = set("+-*/()")


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in _OPS:
            tokens.append(("op", ch))
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            if j >= n:
                raise FormulaError("Unterminated string literal")
            tokens.append(("str", text[i + 1 : j]))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] == "."):
                j += 1
            tokens.append(("num", text[i:j]))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("ident", text[i:j]))
            i = j
            continue
        raise FormulaError(f"Unexpected character in formula: {ch!r}")
    return tokens


# ---------------------------------------------------------------------------
# Parser / evaluator
# ---------------------------------------------------------------------------


def _to_number(value: Any):
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


class _Evaluator:
    def __init__(self, tokens: list[tuple[str, str]], record: Mapping[str, Any]):
        self.tokens = tokens
        self.pos = 0
        self.record = record

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _next(self):
        token = self._peek()
        self.pos += 1
        return token

    def expr(self):
        value = self.term()
        while self._peek() == ("op", "+") or self._peek() == ("op", "-"):
            _, op = self._next()
            right = self.term()
            if op == "+":
                left_n, right_n = _to_number(value), _to_number(right)
                if left_n is not None and right_n is not None:
                    value = left_n + right_n
                else:
                    value = str(value) + str(right)
            else:
                value = self._arith(value, right, "-")
        return value

    def term(self):
        value = self.factor()
        while self._peek() == ("op", "*") or self._peek() == ("op", "/"):
            _, op = self._next()
            right = self.factor()
            value = self._arith(value, right, op)
        return value

    def factor(self):
        kind, text = self._next()
        if kind == "num":
            return _to_number(text)
        if kind == "str":
            return text
        if kind == "ident":
            return self.record.get(text, "")
        if (kind, text) == ("op", "("):
            value = self.expr()
            if self._next() != ("op", ")"):
                raise FormulaError("Missing closing parenthesis")
            return value
        if (kind, text) == ("op", "-"):
            inner = _to_number(self.factor())
            if inner is None:
                raise FormulaError("Cannot negate a non-number")
            return -inner
        raise FormulaError(f"Unexpected token: {text!r}")

    @staticmethod
    def _arith(left, right, op):
        left_n, right_n = _to_number(left), _to_number(right)
        if left_n is None or right_n is None:
            raise FormulaError(f"Non-numeric operand for {op!r}")
        if op == "-":
            return left_n - right_n
        if op == "*":
            return left_n * right_n
        if right_n == 0:
            raise FormulaError("Division by zero")
        return left_n / right_n


def _result_to_string(value: Any) -> str:
    number = _to_number(value) if not isinstance(value, str) else None
    if isinstance(value, str):
        return value
    if number is None:
        return str(value)
    if isinstance(number, int):
        return str(number)
    return str(int(number)) if float(number).is_integer() else str(number)


def evaluate_formula(formula: str, record: Mapping[str, Any]) -> str:
    """Evaluate one formula against a record; raises FormulaError on problems."""
    evaluator = _Evaluator(_tokenize(formula), record)
    value = evaluator.expr()
    if evaluator.pos != len(evaluator.tokens):
        raise FormulaError("Trailing tokens in formula")
    return _result_to_string(value)


# ---------------------------------------------------------------------------
# Field helpers (used by object_records)
# ---------------------------------------------------------------------------


def _is_computed(field: Mapping[str, Any]) -> bool:
    return bool(field.get("computed")) or str(field.get("type") or "").lower() == "computed"


def formula_fields(fields: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        f for f in fields
        if _is_computed(f) and isinstance(f.get("formula"), str) and f["formula"].strip()
    ]


def rollup_fields(fields: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    out = []
    for f in fields:
        rollup = f.get("rollup")
        if _is_computed(f) and isinstance(rollup, Mapping):
            if rollup.get("collection") and rollup.get("fk_field") and rollup.get("op") in ROLLUP_OPS:
                out.append(f)
    return out


def apply_formulas(fields: Iterable[Mapping[str, Any]], record: dict[str, Any]) -> dict[str, Any]:
    """Return record with every formula field recomputed (rollup values on the
    record are visible to formulas -- callers apply rollups first)."""
    specs = formula_fields(fields)
    if not specs:
        return record
    out = dict(record)
    for field in specs:
        name = field["name"]
        try:
            out[name] = evaluate_formula(field["formula"], out)
        except FormulaError:
            out[name] = ""
    return out


def compute_rollup(rollup: Mapping[str, Any], children: list[Mapping[str, Any]]) -> str:
    """Aggregate child rows per the rollup spec; returns the stored string."""
    where = rollup.get("where")
    if isinstance(where, Mapping) and where:
        children = [
            c for c in children
            if all(str(c.get(k, "")) == str(v) for k, v in where.items())
        ]
    op = rollup["op"]
    if op == "count":
        return str(len(children))
    values = []
    for child in children:
        number = _to_number(child.get(rollup.get("field", ""), ""))
        if number is not None:
            values.append(number)
    if not values:
        return "0" if op == "sum" else ""
    if op == "sum":
        return _result_to_string(sum(values))
    if op == "min":
        return _result_to_string(min(values))
    if op == "max":
        return _result_to_string(max(values))
    return _result_to_string(sum(values) / len(values))
