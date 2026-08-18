#!/usr/bin/env python3
"""Parse Qt headers with libclang into a JSON intermediate representation.

This replaces smoke/smokegen's custom C++ parser. The IR captures everything
the Ruby binding codegen needs: classes, inheritance, methods (with argument
types and defaults), enums, signals and slots.

Signals/slots are detected by redefining Qt's QT_ANNOTATE_ACCESS_SPECIFIER
macro so Q_SIGNALS:/Q_SLOTS: sections mark their members with clang
`annotate` attributes ("qt_signal", "qt_slot").

Usage:
    python3 parse_qt.py --qt-prefix /opt/homebrew/opt/qt \
        --modules QtCore QtWidgets -o qt_ir.json
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from clang import cindex
from clang.cindex import CursorKind, AccessSpecifier, TypeKind


def find_libclang():
    """Locate a libclang shared library. Prefer the system toolchain's copy:
    it matches the SDK headers, unlike the PyPI-bundled libclang which
    mis-parses newer libc++."""
    for candidate in (
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
        "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
        "/usr/lib/llvm/lib/libclang.so",
    ):
        if Path(candidate).exists():
            return candidate
    try:
        import clang as clang_pkg
        bundled = Path(clang_pkg.__file__).parent / "native" / "libclang.dylib"
        if bundled.exists():
            return str(bundled)
    except ImportError:
        pass
    return None  # let cindex try its defaults


ANNOTATION_DEFINES = [
    # Q_SIGNALS: / Q_SLOTS: sections annotate their members
    "-DQT_ANNOTATE_ACCESS_SPECIFIER(x)=__attribute__((annotate(#x)))",
    # Q_PROPERTY/Q_ENUM/etc. class-info annotations
    "-DQT_ANNOTATE_CLASS(type,...)=static_assert(sizeof(#__VA_ARGS__),#type);",
    "-DQT_ANNOTATE_CLASS2(type,a1,a2)=static_assert(sizeof(#a1,#a2),#type);",
]


def default_sysroot_args():
    """The bundled libclang has no default SDK/stdlib search paths."""
    args = []
    if sys.platform == "darwin":
        import subprocess
        try:
            sdk = subprocess.run(["xcrun", "--show-sdk-path"], check=True,
                                 capture_output=True, text=True).stdout.strip()
            args += ["-isysroot", sdk]
            cxx_inc = Path(sdk).parent.parent.parent / "usr" / "include" / "c++" / "v1"
            if cxx_inc.exists():
                args.append(f"-I{cxx_inc}")
        except (OSError, subprocess.CalledProcessError):
            pass
        try:
            resource_dir = subprocess.run(
                ["xcrun", "clang", "-print-resource-dir"], check=True,
                capture_output=True, text=True).stdout.strip()
            args.append(f"-I{Path(resource_dir) / 'include'}")
        except (OSError, subprocess.CalledProcessError):
            pass
    return args


def parse_translation_unit(qt_prefix: Path, modules, extra_args=()):
    """Parse a synthetic TU that includes each requested Qt module."""
    include_dir = qt_prefix / "include"
    lib_dir = qt_prefix / "lib"
    args = [
        "-x", "c++", "-std=c++20", "-fPIC",
        "-fparse-all-comments",
        f"-I{include_dir}",
        f"-F{lib_dir}",  # macOS framework-style Qt
    ]
    args += default_sysroot_args()
    args += ANNOTATION_DEFINES
    args += list(extra_args)

    source = "".join(f"#include <{m}/{m}>\n" for m in modules)
    index = cindex.Index.create()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".cpp", delete=False) as f:
        f.write(source)
        tu_path = f.name
    tu = index.parse(
        tu_path, args=args,
        options=cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES,
    )
    fatal = [d for d in tu.diagnostics if d.severity >= cindex.Diagnostic.Error]
    if fatal:
        for d in fatal[:20]:
            print(f"error: {d.spelling} @ {d.location}", file=sys.stderr)
        if any(d.severity >= cindex.Diagnostic.Fatal for d in fatal):
            sys.exit(1)
    return tu


def type_to_dict(t):
    return {
        "spelling": t.spelling,
        "canonical": t.get_canonical().spelling,
        "kind": t.kind.name,
    }


def collect_annotations(cursor):
    return [c.spelling for c in cursor.get_children()
            if c.kind == CursorKind.ANNOTATE_ATTR]


def method_to_dict(cursor, annotations):
    args = []
    for arg in cursor.get_arguments():
        default = None
        # Default values are the tokens after '=' in the parameter declaration
        tokens = [t.spelling for t in arg.get_tokens()]
        if "=" in tokens:
            default = " ".join(tokens[tokens.index("=") + 1:])
        args.append({
            "name": arg.spelling or None,
            "type": type_to_dict(arg.type),
            "default": default,
        })
    kind = "method"
    if "qt_signal" in annotations:
        kind = "signal"
    elif "qt_slot" in annotations:
        kind = "slot"
    return {
        "name": cursor.spelling,
        "kind": kind,
        "result_type": type_to_dict(cursor.result_type),
        "args": args,
        "access": cursor.access_specifier.name.lower(),
        "const": cursor.is_const_method(),
        "static": cursor.is_static_method(),
        "virtual": cursor.is_virtual_method(),
        "pure_virtual": cursor.is_pure_virtual_method(),
    }


def enum_to_dict(cursor):
    return {
        "name": cursor.spelling,
        "scoped": cursor.is_scoped_enum(),
        "values": {c.spelling: c.enum_value for c in cursor.get_children()
                   if c.kind == CursorKind.ENUM_CONSTANT_DECL},
    }


def class_to_dict(cursor):
    bases, methods, enums, ctors = [], [], [], []
    destructor = None
    abstract = False
    for child in cursor.get_children():
        if child.kind == CursorKind.CXX_BASE_SPECIFIER:
            if child.access_specifier == AccessSpecifier.PUBLIC:
                bases.append(child.type.spelling)
        elif child.kind == CursorKind.CXX_METHOD:
            if child.access_specifier == AccessSpecifier.PRIVATE:
                continue
            annotations = collect_annotations(child)
            m = method_to_dict(child, annotations)
            if m["pure_virtual"]:
                abstract = True
            methods.append(m)
        elif child.kind == CursorKind.CONSTRUCTOR:
            if child.access_specifier != AccessSpecifier.PRIVATE:
                ctors.append(method_to_dict(child, []))
        elif child.kind == CursorKind.DESTRUCTOR:
            destructor = {"access": child.access_specifier.name.lower(),
                          "virtual": child.is_virtual_method()}
        elif child.kind == CursorKind.ENUM_DECL and child.spelling:
            if child.access_specifier != AccessSpecifier.PRIVATE:
                enums.append(enum_to_dict(child))
    return {
        "name": cursor.spelling,
        "qualified_name": qualified_name(cursor),
        "bases": bases,
        "abstract": abstract,
        "constructors": ctors,
        "destructor": destructor,
        "methods": methods,
        "enums": enums,
    }


def qualified_name(cursor):
    parts = []
    c = cursor
    while c is not None and c.kind != CursorKind.TRANSLATION_UNIT:
        if c.spelling:
            parts.append(c.spelling)
        c = c.semantic_parent
    return "::".join(reversed(parts))


def in_qt_headers(cursor, qt_prefix: Path):
    loc = cursor.location
    if loc.file is None:
        return False
    # Compare unresolved paths: brew's qt prefix symlinks into split kegs
    # (qtbase, qtdeclarative, ...), so resolving would escape the prefix
    return loc.file.name.startswith(str(qt_prefix))


def walk(tu, qt_prefix: Path):
    classes, enums = {}, {}

    def visit(cursor):
        for child in cursor.get_children():
            if not in_qt_headers(child, qt_prefix):
                continue
            if child.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL):
                # Only fully-defined, exported-looking, named public classes
                if child.is_definition() and child.spelling.startswith("Q"):
                    qname = qualified_name(child)
                    if qname not in classes:
                        classes[qname] = class_to_dict(child)
            elif child.kind == CursorKind.ENUM_DECL and child.spelling:
                qname = qualified_name(child)
                if qname not in enums and child.is_definition():
                    enums[qname] = enum_to_dict(child)
            elif child.kind == CursorKind.NAMESPACE:
                visit(child)

    visit(tu.cursor)
    return classes, enums


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qt-prefix", type=Path, required=True,
                    help="Qt installation prefix (e.g. /opt/homebrew/opt/qt)")
    ap.add_argument("--modules", nargs="+", default=["QtCore"],
                    help="Qt modules to parse (e.g. QtCore QtWidgets)")
    ap.add_argument("--libclang", help="Path to libclang shared library")
    ap.add_argument("--clang-arg", action="append", default=[],
                    help="Extra argument to pass to clang (repeatable)")
    ap.add_argument("-o", "--output", type=Path, default=Path("qt_ir.json"))
    opts = ap.parse_args()

    libclang = opts.libclang or find_libclang()
    if libclang:
        cindex.Config.set_library_file(libclang)

    tu = parse_translation_unit(opts.qt_prefix, opts.modules, opts.clang_arg)
    classes, enums = walk(tu, opts.qt_prefix)

    ir = {
        "qt_prefix": str(opts.qt_prefix),
        "modules": opts.modules,
        "classes": classes,
        "enums": enums,
    }
    opts.output.write_text(json.dumps(ir, indent=1))
    n_methods = sum(len(c["methods"]) for c in classes.values())
    n_signals = sum(1 for c in classes.values()
                    for m in c["methods"] if m["kind"] == "signal")
    print(f"{len(classes)} classes, {n_methods} methods "
          f"({n_signals} signals), {len(enums)} free enums -> {opts.output}")


if __name__ == "__main__":
    main()
