"""Candidate source compatibility and explicit attribute guards, not a sandbox.

Sealed sources and explicit AST attribute accesses are guarded. Indirect traversal
(e.g. format strings) and arbitrary same-interpreter compromise are not excluded.
Secrets belong to the parent and sealed paths, protected by validated OS isolation.
"""

import ast
import builtins
import importlib.abc
import importlib.util
import operator
import os
import sys
import types

# Reviewed stdlib capabilities needed by ordinary solutions to the nine dev tasks.
# Do not expose module internals, import machinery, process control or raw stdout fds.
CAPABILITIES = {
    "__future__": {"annotations"},
    "statistics": {"median", "mean", "median_low", "median_high", "StatisticsError"},
    "json": {"dump", "dumps", "load", "loads", "JSONDecodeError"},
    "copy": {"copy", "deepcopy"},
    "os": {
        "path",
        "fdopen",
        "fsync",
        "replace",
        "unlink",
        "remove",
        "mkdir",
        "makedirs",
        "listdir",
        "stat",
    },
    "posixpath": {
        "join",
        "exists",
        "isfile",
        "isdir",
        "dirname",
        "basename",
        "abspath",
        "normpath",
        "split",
        "splitext",
    },
    "tempfile": {"mkstemp", "mkdtemp", "TemporaryDirectory", "NamedTemporaryFile"},
    "io": {"BytesIO", "StringIO"},
    "http": {"HTTPStatus"},
    "urllib": {"parse"},
    "urllib.parse": {
        "parse_qs",
        "parse_qsl",
        "urlencode",
        "quote",
        "unquote",
        "urlparse",
    },
    "unittest": {"TestCase", "main", "skip", "skipIf", "skipUnless", "expectedFailure"},
    "dataclasses": {"dataclass", "field", "asdict", "astuple", "replace"},
    "typing": {
        "Any",
        "Optional",
        "Union",
        "Iterable",
        "Iterator",
        "Sequence",
        "Mapping",
        "Callable",
        "List",
        "Dict",
        "Tuple",
        "Set",
    },
    "decimal": {
        "Decimal",
        "ROUND_HALF_UP",
        "ROUND_HALF_EVEN",
        "InvalidOperation",
        "localcontext",
        "getcontext",
    },
    "operator": {"attrgetter", "itemgetter", "add", "sub", "mul", "truediv"},
    "math": {
        "ceil",
        "floor",
        "sqrt",
        "isfinite",
        "isnan",
        "isclose",
        "fabs",
        "fsum",
        "inf",
        "nan",
        "pi",
    },
    "re": {
        "sub",
        "subn",
        "match",
        "search",
        "fullmatch",
        "findall",
        "finditer",
        "split",
        "compile",
        "escape",
        "IGNORECASE",
        "I",
        "MULTILINE",
        "M",
        "DOTALL",
        "S",
        "ASCII",
        "A",
    },
    "collections": {"Counter", "defaultdict", "deque", "OrderedDict", "namedtuple"},
    "itertools": {
        "chain",
        "islice",
        "groupby",
        "product",
        "combinations",
        "permutations",
        "repeat",
        "count",
        "zip_longest",
    },
    "functools": {
        "reduce",
        "partial",
        "lru_cache",
        "cache",
        "wraps",
        "cached_property",
    },
    "string": {
        "ascii_letters",
        "ascii_lowercase",
        "ascii_uppercase",
        "digits",
        "punctuation",
        "whitespace",
    },
}
# Import and observation safety are separate from OS confinement. Public stdlib
# APIs are broadly available; explicit interpreter/process-control APIs remain guarded.
UNSAFE_MODULES = {
    "sys",
    "gc",
    "inspect",
    "ctypes",
    "builtins",
    "importlib",
    "atexit",
    "threading",
    "_thread",
    "signal",
    "subprocess",
    "multiprocessing",
    "runpy",
    "marshal",
    "code",
    "codeop",
    "site",
}
PURE_STDLIB = {
    "abc",
    "array",
    "base64",
    "binascii",
    "bisect",
    "calendar",
    "collections",
    "contextlib",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "fractions",
    "functools",
    "hashlib",
    "heapq",
    "hmac",
    "http",
    "io",
    "itertools",
    "logging",
    "math",
    "numbers",
    "operator",
    "pathlib",
    "random",
    "re",
    "statistics",
    "string",
    "struct",
    "textwrap",
    "time",
    "typing",
    "unicodedata",
    "urllib",
    "uuid",
    "warnings",
    "weakref",
    "zoneinfo",
    "__future__",
    "tempfile",
}

FORBIDDEN_NAMES = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "dir",
    "setattr",
    "delattr",
    "breakpoint",
    "help",
    "input",
    "memoryview",
    "__builtins__",
    "__loader__",
    "__spec__",
    "__package__",
}
FORBIDDEN_ATTRS = {
    "f_back",
    "f_globals",
    "f_locals",
    "f_builtins",
    "f_code",
    "tb_frame",
    "tb_next",
    "gi_frame",
    "gi_code",
    "cr_frame",
    "cr_code",
    "ag_frame",
    "mro",
}
SAFE_DEFS = {
    "__init__",
    "__repr__",
    "__str__",
    "__len__",
    "__iter__",
    "__next__",
    "__eq__",
    "__lt__",
}


class IntegrityError(Exception):
    pass


def forbidden_attribute(name):
    return name.startswith(("__", "_llme_")) or name in FORBIDDEN_ATTRS


def violations(source):
    """Facts from source only. Syntax errors remain ordinary candidate execution failures."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    bad = []
    for n in ast.walk(tree):
        for field, value in ast.iter_fields(n):
            if field in {"arg", "name", "asname", "rest", "names"}:
                identifiers = value if isinstance(value, list) else [value]
                if any(
                    isinstance(v, str) and v.startswith("_llme_") for v in identifiers
                ):
                    bad.append(f"line {n.lineno}: reserved binding")
        if isinstance(n, ast.Name) and (
            n.id in FORBIDDEN_NAMES or n.id.startswith("_llme_")
        ):
            bad.append(f"line {n.lineno}: name {n.id}")
        elif isinstance(n, ast.Attribute) and forbidden_attribute(n.attr):
            bad.append(f"line {n.lineno}: attribute {n.attr}")
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n.name.startswith("_llme_"):
                bad.append(f"line {n.lineno}: definition {n.name}")
        elif isinstance(n, ast.Import):
            for alias in n.names:
                if alias.name.split(".")[0] in {
                    "sys",
                    "gc",
                    "inspect",
                    "ctypes",
                    "builtins",
                    "importlib",
                    "atexit",
                    "threading",
                    "_thread",
                    "signal",
                    "subprocess",
                    "multiprocessing",
                }:
                    bad.append(f"line {n.lineno}: import {alias.name}")
        elif isinstance(n, ast.ImportFrom):
            if any(forbidden_attribute(a.name) for a in n.names):
                bad.append(f"line {n.lineno}: reflective import")
        elif isinstance(n, ast.MatchClass) and n.kwd_attrs:
            bad.append(f"line {n.lineno}: class pattern attributes")
    return sorted(set(bad))


class GuardAttributes(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            guarded = ast.Call(
                ast.Name("_llme_get", ast.Load()),
                [node.value, ast.Constant(node.attr)],
                [],
            )
        else:
            # A guarded subscription target retains Python's own assignment order,
            # unpacking, in-place operators and single target evaluation.
            guarded = ast.Subscript(
                ast.Call(
                    ast.Name("_llme_slot", ast.Load()),
                    [node.value, ast.Constant(node.attr)],
                    [],
                ),
                ast.Constant(0),
                node.ctx,
            )
        return ast.copy_location(guarded, node)


class AttributeSlot:
    def __init__(self, boundary, obj, name):
        self.boundary, self.obj, self.name = boundary, obj, name

    def __getitem__(self, index):
        return self.boundary.get(self.obj, self.name)

    def __setitem__(self, index, value):
        self.boundary.set(self.obj, self.name, value)

    def __delitem__(self, index):
        self.boundary.writable(self.obj, self.name)
        delattr(self.obj, self.name)


class ModuleView:
    """Import-from must receive the guarded value too, not a raw stdlib member."""

    def __init__(self, module, boundary):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_boundary", boundary)

    def __getattribute__(self, name):
        if name == "__class__":
            return ModuleView  # CPython isinstance fallback; source dunder access is denied.
        return object.__getattribute__(self, "_boundary").get(
            object.__getattribute__(self, "_module"), name
        )


class CandidateBoundary(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources, record, protected_fds=()):

        collisions = set(sources) & set(sys.modules)
        if collisions:
            record.append("module name collision " + min(collisions))
            raise IntegrityError(record[-1])
        self.protected_fds = {0, 1, 2, *protected_fds}
        self.sources = sources
        self.record = record
        self.modules = set()
        self.real_import = builtins.__import__

    def deny(self, message):
        self.record.append(message)
        raise IntegrityError(message)

    def get(self, obj, name):
        if forbidden_attribute(name):
            self.deny(f"attribute {name}")
        if type(obj) is ModuleView:
            return self.get(object.__getattribute__(obj, "_module"), name)
        if (
            isinstance(obj, types.ModuleType)
            and obj.__name__ not in self.modules
            and (obj.__name__.split(".")[0] not in PURE_STDLIB or name.startswith("_"))
            and name not in CAPABILITIES.get(obj.__name__, set())
        ):
            self.deny(f"module capability {obj.__name__}.{name}")
        value = getattr(obj, name)
        if value is operator.attrgetter:

            def attrgetter(*names):
                if any(
                    forbidden_attribute(part) for n in names for part in n.split(".")
                ):
                    self.deny("reflective attrgetter")

                # Do not let attrgetter bypass module capability checks.
                def getattrs(o):
                    vals = []
                    for n in names:
                        v = o
                        for part in n.split("."):
                            v = self.get(v, part)
                        vals.append(v)
                    return vals[0] if len(vals) == 1 else tuple(vals)

                return getattrs

            return attrgetter
        if obj is os and name == "fdopen":

            def fdopen(fd, *args, **kwargs):
                number = (
                    int.__index__(fd) if isinstance(fd, int) else operator.index(fd)
                )
                if number in self.protected_fds:
                    self.deny("raw fd fdopen")
                return value(number, *args, **kwargs)

            return fdopen
        return self.view(value)

    def writable(self, obj, name):
        if (
            forbidden_attribute(name)
            or type(obj) is ModuleView
            or isinstance(obj, (types.ModuleType, types.FunctionType, type))
        ):
            self.deny(f"foreign attribute write {name}")
        if type(obj).__module__ not in self.modules:
            self.deny(f"foreign object write {name}")

    def set(self, obj, name, value):
        self.writable(obj, name)
        setattr(obj, name, value)

    def slot(self, obj, name):
        return AttributeSlot(self, obj, name)

    def view(self, module):
        if (
            type(module) is ModuleView
            or not isinstance(module, types.ModuleType)
            or module.__name__ in self.modules
        ):
            return module
        return ModuleView(module, self)

    def hasattr(self, obj, name):
        try:
            self.get(obj, name)
        except AttributeError:
            return False
        return True

    def getattr(self, obj, name, *default):
        try:
            return self.get(obj, name)
        except AttributeError:
            if default:
                return default[0]
            raise

    def opening(self, file, *args, **kwargs):
        if isinstance(file, int) or os.path.realpath(file).startswith(
            ("/dev/", "/proc/")
        ):
            self.deny("raw fd open")
        return builtins.open(file, *args, **kwargs)

    def importing(self, name, globals=None, locals=None, fromlist=(), level=0):
        absolute = (
            importlib.util.resolve_name("." * level + name, globals["__package__"])
            if level
            else name
        )
        if (
            absolute not in CAPABILITIES
            and absolute not in self.sources
            and absolute.split(".")[0] not in PURE_STDLIB
        ):
            self.deny(f"import capability {absolute}")
        module = self.real_import(name, globals, locals, fromlist, level)
        if fromlist:
            for attr in fromlist:
                if attr == "*":
                    self.deny("star import")
                # Import-from emits no Attribute node, so guard it here too.
                self.get(module, attr)
        return self.view(module)

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.sources:
            _source, filename, package = self.sources[fullname]
            return importlib.util.spec_from_loader(
                fullname, self, origin=filename, is_package=package
            )
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source, filename, package = self.sources[module.__name__]
        bad = violations(source)
        if bad:
            self.deny(f"{filename}: {bad[0]}")
        tree = ast.fix_missing_locations(
            GuardAttributes().visit(ast.parse(source, filename))
        )
        if any(
            isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del))
            for n in ast.walk(tree)
        ):
            self.deny("unsupported attribute target")
        private = dict(vars(builtins))
        for name in FORBIDDEN_NAMES:
            private.pop(name, None)
        private["__import__"] = self.importing
        private["open"] = self.opening
        private["getattr"] = self.getattr
        private["hasattr"] = self.hasattr
        # Preserve native type identity/isinstance semantics. Access to returned
        # type objects still goes through GuardAttributes, including aliases.
        private["type"] = builtins.type
        module.__dict__.update(
            __builtins__=private,
            _llme_get=self.get,
            _llme_slot=self.slot,
            __file__=filename,
        )
        if package:
            module.__path__ = []
        self.modules.add(module.__name__)
        exec(compile(tree, filename, "exec"), module.__dict__)  # noqa: S102


def signals(source):
    """Non-decisive observations; never promote string/dunder heuristics to a reject."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    flags = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "{" in node.value
            and "__" in node.value
        ):
            flags.add("format_introspection_signal")
        if isinstance(node, ast.FunctionDef) and node.name in {
            "__getattribute__",
            "__del__",
            "__reduce__",
        }:
            flags.add("custom_runtime_hook_signal")
    return sorted(flags)
