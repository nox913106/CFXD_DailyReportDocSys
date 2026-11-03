"""範本用到的 Excel 公式子集合的求值器：
參照（含跨工作表、$ 絕對參照）、範圍、+ - * / & 比較運算、
SUM、COUNTIF、COUNTIFS、TEXT、DATE、IF、IFERROR。
不支援的公式丟出 FormulaError，由呼叫端改用快取值。"""

import datetime as dt
import re

from openpyxl.utils import column_index_from_string, get_column_letter


class FormulaError(Exception):
    pass


TOKEN = re.compile(
    r"""\s*(?:
    (?P<str>"(?:[^"]|"")*")|
    (?P<ref>(?:'[^']+'|[A-Za-z_][\w.]*)?!?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)|
    (?P<num>\d+(?:\.\d+)?)|
    (?P<bool>TRUE|FALSE)\b|
    (?P<func>[A-Z][A-Z0-9.]*)\(|
    (?P<op><>|<=|>=|[-+*/&=<>(),:])
    )""",
    re.X,
)


def tokenize(s):
    pos, out = 0, []
    s = s.strip()
    while pos < len(s):
        m = TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"無法解析: {s[pos:pos + 20]}")
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "ref" and "!" not in val and not re.fullmatch(r"\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?", val):
            raise FormulaError(f"不支援的名稱: {val}")
        out.append((kind, val))
        pos = m.end()
    return out


EXCEL_EPOCH = dt.datetime(1899, 12, 30)


def to_number(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dt.datetime):
        return (v - EXCEL_EPOCH).total_seconds() / 86400
    if isinstance(v, dt.date):
        return float((v - EXCEL_EPOCH.date()).days)
    if isinstance(v, dt.time):
        return (v.hour * 3600 + v.minute * 60 + v.second) / 86400
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        raise FormulaError(f"#VALUE! {v!r}")


def to_text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class Evaluator:
    def __init__(self, get_value):
        """get_value(sheet, 'A1') -> 已求值的儲存格值"""
        self.get = get_value

    def eval(self, formula, sheet):
        self.toks = tokenize(formula.lstrip("="))
        self.i = 0
        self.sheet = sheet
        v = self.expr()
        if self.i != len(self.toks):
            raise FormulaError("多餘的符號")
        return v

    # 遞迴下降：比較 < 串接 < 加減 < 乘除 < 一元 < 基本
    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def expr(self):
        left = self.concat()
        while self.peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            right = self.concat()
            left = _compare(left, right, op)
        return left

    def concat(self):
        left = self.additive()
        while self.peek()[1] == "&":
            self.take()
            left = to_text(_scalar(left)) + to_text(_scalar(self.additive()))
        return left

    def additive(self):
        left = self.term()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            right = self.term()
            a, b = to_number(_scalar(left)), to_number(_scalar(right))
            left = a + b if op == "+" else a - b
        return left

    def term(self):
        left = self.unary()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            right = self.unary()
            a, b = to_number(_scalar(left)), to_number(_scalar(right))
            if op == "/" and b == 0:
                raise FormulaError("#DIV/0!")
            left = a * b if op == "*" else a / b
        return left

    def unary(self):
        if self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            v = self.unary()
            return v if op == "+" and not isinstance(_scalar(v), (int, float)) else (
                to_number(_scalar(v)) * (-1 if op == "-" else 1))
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return float(val)
        if kind == "str":
            return val[1:-1].replace('""', '"')
        if kind == "bool":
            return val == "TRUE"
        if kind == "ref":
            return self.ref(val)
        if kind == "func":
            args = []
            if self.peek()[1] != ")":
                while True:
                    args.append(self.expr())
                    if self.peek()[1] == ",":
                        self.take()
                        continue
                    break
            if self.take()[1] != ")":
                raise FormulaError("缺少 )")
            return self.call(val, args)
        if val == "(":
            v = self.expr()
            if self.take()[1] != ")":
                raise FormulaError("缺少 )")
            return v
        raise FormulaError(f"未預期的符號 {val}")

    def ref(self, text):
        if "!" in text:
            sheet, rng = text.rsplit("!", 1)
            sheet = sheet.strip("'")
        else:
            sheet, rng = self.sheet, text
        rng = rng.replace("$", "")
        if ":" in rng:
            a, b = rng.split(":")
            ca, ra = _split(a)
            cb, rb = _split(b)
            return [[self.get(sheet, f"{get_column_letter(c)}{r}") for c in range(ca, cb + 1)]
                    for r in range(ra, rb + 1)]
        return self.get(sheet, rng)

    def call(self, name, args):
        flat = lambda v: [x for row in v for x in row] if isinstance(v, list) else [v]
        if name == "SUM":
            return sum(to_number(x) for a in args for x in flat(a) if isinstance(x, (int, float)) and not isinstance(x, bool))
        if name == "COUNTIF":
            return float(sum(1 for x in flat(args[0]) if _match(x, _scalar(args[1]))))
        if name == "COUNTIFS":
            ranges = [flat(args[k]) for k in range(0, len(args), 2)]
            crits = [_scalar(args[k]) for k in range(1, len(args), 2)]
            return float(sum(1 for row in zip(*ranges) if all(_match(x, c) for x, c in zip(row, crits))))
        if name == "DATE":
            y, m, d = (int(to_number(_scalar(a))) for a in args)
            return dt.datetime(y, m, d)
        if name == "TEXT":
            v, fmt = to_number(_scalar(args[0])), to_text(_scalar(args[1]))
            if re.fullmatch(r"0+", fmt):
                return str(int(round(v))).zfill(len(fmt))
            raise FormulaError(f"TEXT 格式不支援 {fmt}")
        if name == "IF":
            cond = _scalar(args[0])
            truthy = bool(to_number(cond)) if not isinstance(cond, str) else bool(cond)
            if truthy:
                return args[1] if len(args) > 1 else True
            return args[2] if len(args) > 2 else False
        if name == "IFERROR":
            return args[0]
        raise FormulaError(f"不支援的函數 {name}")


def _split(a):
    m = re.fullmatch(r"([A-Z]{1,3})(\d+)", a)
    return column_index_from_string(m.group(1)), int(m.group(2))


def _scalar(v):
    if isinstance(v, list):
        return v[0][0] if v and v[0] else None
    return v


def _match(x, crit):
    if isinstance(crit, bool):
        return x is crit or x == crit and isinstance(x, bool)
    if isinstance(crit, (int, float)):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and float(x) == float(crit)
    return to_text(x).lower() == to_text(crit).lower()


def _compare(a, b, op):
    a, b = _scalar(a), _scalar(b)
    if isinstance(a, str) or isinstance(b, str):
        a, b = to_text(a).lower(), to_text(b).lower()
    else:
        a, b = to_number(a), to_number(b)
    return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
