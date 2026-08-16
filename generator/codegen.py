#!/usr/bin/env python3
"""Generate C++ Ruby-extension glue for Qt classes, straight off the clang AST.

Consumes cursors from parse_qt.parse_translation_unit (no intermediate IR).
Emits one .cpp file registering each requested class under the Ruby `Qt`
module: constructors, methods (snake_cased, overloads dispatched by arity),
enums as constants, and signals as `on_<signal> { }` methods implemented with
compile-time member-pointer QObject::connect — no moc involved.

Usage:
    python3 codegen.py --qt-prefix /opt/homebrew/opt/qt \
        --modules QtCore --classes QObject QTimer -o qt6_generated.cpp
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_qt import (cindex, CursorKind, AccessSpecifier, TypeKind,
                      parse_translation_unit, find_libclang, in_qt_headers,
                      collect_annotations)

INT_TYPES = {
    "int": ("NUM2INT", "INT2NUM"),
    "unsigned int": ("NUM2UINT", "UINT2NUM"),
    "short": ("NUM2SHORT", "INT2NUM"),
    "unsigned short": ("NUM2USHORT", "UINT2NUM"),
    "long": ("NUM2LONG", "LONG2NUM"),
    "unsigned long": ("NUM2ULONG", "ULONG2NUM"),
    "long long": ("NUM2LL", "LL2NUM"),
    "unsigned long long": ("NUM2ULL", "ULL2NUM"),
    "char": ("NUM2CHR", "INT2NUM"),
}


def snake(name):
    s = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)
    return s.lower()


class Type:
    """Classification of a clang type for marshalling purposes."""
    def __init__(self, kind, cxx, to_cxx=None, to_rb=None):
        self.kind = kind      # void|bool|int|float|enum|qstring|qbytearray|cstr|objptr|unsupported
        self.cxx = cxx        # C++ type spelling to declare locals with
        self.to_cxx = to_cxx  # fmt string, {} = VALUE expr
        self.to_rb = to_rb    # fmt string, {} = C++ expr


def classify(t, known_classes):
    canon = t.get_canonical()
    spelling = canon.spelling
    if canon.kind == TypeKind.VOID:
        return Type("void", "void")
    if canon.kind == TypeKind.BOOL:
        return Type("bool", "bool", "RTEST({})", "({}) ? Qtrue : Qfalse")
    if spelling in INT_TYPES:
        n2, i2 = INT_TYPES[spelling]
        return Type("int", spelling, n2 + "({})", i2 + "({})")
    if canon.kind in (TypeKind.DOUBLE, TypeKind.FLOAT):
        return Type("float", spelling, "NUM2DBL({})", "DBL2NUM({})")
    if canon.kind == TypeKind.ENUM:
        return Type("enum", spelling,
                    "static_cast<%s>(NUM2INT({}))" % spelling,
                    "INT2NUM(static_cast<int>({}))")
    # Strip const-reference down to the value type
    value = canon
    if canon.kind == TypeKind.LVALUEREFERENCE:
        value = canon.get_pointee()
        if not value.is_const_qualified():
            return Type("unsupported", spelling)  # out-params unsupported
    vspell = value.spelling.replace("const ", "").strip()
    if vspell == "QString":
        return Type("qstring", "QString",
                    "qt6rb::to_qstring({})", "qt6rb::from_qstring({})")
    if vspell == "QByteArray":
        return Type("qbytearray", "QByteArray",
                    "qt6rb::to_qbytearray({})", "qt6rb::from_qbytearray({})")
    if canon.kind == TypeKind.POINTER:
        pointee = canon.get_pointee()
        pspell = pointee.spelling.replace("const ", "").strip()
        if pspell == "char" and pointee.is_const_qualified():
            return Type("cstr", "const char*", "StringValueCStr({})", "rb_str_new_cstr({})")
        if pspell in known_classes:
            typ = Type("objptr", f"{pspell}*",
                       "static_cast<%s*>(qt6rb::unwrap({}, &cls_%s))" % (pspell, pspell),
                       "qt6rb::wrap_qobject(({}), &cls_%s)" % pspell)
            typ.cls = pspell
            return typ
    return Type("unsupported", spelling)


class Method:
    def __init__(self, cursor, klass, known):
        self.cursor = cursor
        self.name = cursor.spelling
        self.static = cursor.is_static_method() if cursor.kind == CursorKind.CXX_METHOD else False
        self.result = (classify(cursor.result_type, known)
                       if cursor.kind == CursorKind.CXX_METHOD else None)
        args = [a for a in cursor.get_arguments()
                if not a.type.spelling.endswith("QPrivateSignal")]
        self.params = [classify(a.type, known) for a in args]
        self.min_args = 0
        for a in args:
            tokens = [t.spelling for t in a.get_tokens()]
            if "=" in tokens:
                break
            self.min_args += 1
        self.supported = all(p.kind not in ("unsupported", "void") for p in self.params) \
            and (self.result is None or self.result.kind != "unsupported")


class Klass:
    def __init__(self, cursor):
        self.cursor = cursor
        self.name = cursor.spelling
        self.bases = []
        self.methods = []       # supported CXX methods
        self.signals = []       # supported signals
        self.ctors = []
        self.enums = []
        self.skipped = 0
        self.dtor_public = True
        self.abstract = False


def harvest(tu, qt_prefix, wanted):
    classes = {}

    def visit(cursor):
        for child in cursor.get_children():
            if child.kind == CursorKind.NAMESPACE:
                visit(child)
                continue
            if child.kind not in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL):
                continue
            if not in_qt_headers(child, qt_prefix):
                continue
            if child.spelling in wanted and child.is_definition() \
                    and child.spelling not in classes:
                classes[child.spelling] = Klass(child)

    visit(tu.cursor)
    known = set(classes.keys())

    for k in classes.values():
        for child in k.cursor.get_children():
            if child.kind == CursorKind.CXX_BASE_SPECIFIER:
                base = child.type.spelling
                if base in known:
                    k.bases.append(base)
            elif child.kind == CursorKind.CXX_METHOD:
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method():
                    continue
                if child.is_pure_virtual_method():
                    k.abstract = True
                ann = collect_annotations(child)
                m = Method(child, k, known)
                if "qt_signal" in ann:
                    if m.supported:
                        k.signals.append(m)
                    else:
                        k.skipped += 1
                elif m.supported:
                    k.methods.append(m)
                else:
                    k.skipped += 1
            elif child.kind == CursorKind.CONSTRUCTOR:
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method() or child.is_copy_constructor() \
                        or child.is_move_constructor():
                    continue
                m = Method(child, k, known)
                if m.supported:
                    k.ctors.append(m)
                else:
                    k.skipped += 1
            elif child.kind == CursorKind.DESTRUCTOR:
                k.dtor_public = child.access_specifier == AccessSpecifier.PUBLIC
            elif child.kind == CursorKind.ENUM_DECL and child.spelling:
                if child.access_specifier == AccessSpecifier.PUBLIC:
                    k.enums.append(child)
    return classes


def emit_call(m, klass_name, self_expr, arg_exprs):
    args = ", ".join(t.to_cxx.format(e) for t, e in zip(m.params, arg_exprs))
    if m.cursor.kind == CursorKind.CONSTRUCTOR:
        return f"new {klass_name}({args})"
    if m.static:
        return f"{klass_name}::{m.name}({args})"
    return f"{self_expr}->{m.name}({args})"


def emit_method_group(out, klass, ruby_name, overloads, static):
    """Emit one C function dispatching a group of same-named overloads by arity."""
    cname = f"rb_{klass.name}_{'s_' if static else ''}{ruby_name}".replace("?", "_p").replace("=", "_eq")
    out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
    if not static:
        out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    seen_arities = set()
    for m in overloads:
        for n in range(m.min_args, len(m.params) + 1):
            if n in seen_arities:
                continue
            seen_arities.add(n)
            out.append(f"  if (argc == {n}) {{")
            exprs = [f"argv[{i}]" for i in range(n)]
            call = emit_call(m, klass.name, "o", [
                (m.params[i].to_cxx.format(exprs[i]) if False else exprs[i])
                for i in range(n)])
            # emit_call formats conversions itself; rebuild with only n args
            args = ", ".join(m.params[i].to_cxx.format(exprs[i]) for i in range(n))
            if m.cursor.kind == CursorKind.CONSTRUCTOR:
                call = f"new {klass.name}({args})"
            elif m.static:
                call = f"{klass.name}::{m.name}({args})"
            else:
                call = f"o->{m.name}({args})"
            if m.cursor.kind == CursorKind.CONSTRUCTOR:
                out.append(f"    return qt6rb::wrap_qobject({call}, &cls_{klass.name});")
            elif m.result.kind == "void":
                out.append(f"    {call};")
                out.append("    return Qnil;")
            else:
                out.append(f"    return {m.result.to_rb.format(call)};")
            out.append("  }")
    out.append(f'  rb_raise(rb_eArgError, "wrong number of arguments for {klass.name}#{ruby_name} (%d)", argc);')
    out.append("}")
    out.append("")
    return cname


def emit_signal(out, klass, sig):
    rname = f"on_{snake(sig.name)}"
    cname = f"rb_{klass.name}_{rname}"
    lam_params = ", ".join(f"{t.cxx} a{i}" for i, t in enumerate(sig.params))
    out.append(f"static VALUE {cname}(VALUE self) {{")
    out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    out.append("  VALUE proc = rb_block_proc();")
    out.append("  qt6rb::retain_proc(proc);")
    out.append(f"  QObject::connect(o, &{klass.name}::{sig.name}, o, [proc]({lam_params}) {{")
    if sig.params:
        conv = ", ".join(t.to_rb.format(f"a{i}") for i, t in enumerate(sig.params))
        out.append(f"    VALUE args[] = {{ {conv} }};")
        out.append(f"    qt6rb::call_proc(proc, {len(sig.params)}, args);")
    else:
        out.append("    qt6rb::call_proc(proc, 0, nullptr);")
    out.append("  });")
    out.append("  return self;")
    out.append("}")
    out.append("")
    return rname, cname


def const_name(name):
    return name[0].upper() + name[1:] if name else name


def generate(classes, modules):
    out = []
    out.append("// AUTOGENERATED by generator/codegen.py -- do not edit")
    out.append('#include "qt6_runtime.hpp"')
    for m in modules:
        out.append(f"#include <{m}/{m}>")
    out.append("")
    for k in classes.values():
        if k.dtor_public and not k.abstract:
            deleter = f"[](void* p) {{ delete static_cast<{k.name}*>(p); }}"
        else:
            deleter = "nullptr"
        out.append(f'static qt6rb::ClassInfo cls_{k.name} = {{ "{k.name}", Qnil, {deleter} }};')
    out.append("")

    registrations = []
    for k in classes.values():
        # Constructors
        if k.ctors and not k.abstract:
            groups = {}
            groups.setdefault("new", []).extend(k.ctors)
            cname = emit_method_group(out, k, "new", k.ctors, static=True)
            registrations.append(
                f'  rb_define_singleton_method(cls_{k.name}.rb_class, "new", RUBY_METHOD_FUNC({cname}), -1);')
        # Methods grouped by ruby name
        for static in (False, True):
            groups = {}
            for m in k.methods:
                if m.static != static:
                    continue
                groups.setdefault(snake(m.name), []).append(m)
            for rname, overloads in groups.items():
                cname = emit_method_group(out, k, rname, overloads, static)
                target = f"cls_{k.name}.rb_class"
                if static:
                    registrations.append(
                        f'  rb_define_singleton_method({target}, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')
                else:
                    registrations.append(
                        f'  rb_define_method({target}, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')
                    # setter sugar: set_interval(v) also as interval=
                    if rname.startswith("set_") and any(len(m.params) == 1 for m in overloads):
                        registrations.append(
                            f'  rb_define_alias({target}, "{rname[4:]}=", "{rname}");')
        # Signals (skip overloaded ones -- member pointer would be ambiguous)
        sig_names = {}
        for s in k.signals:
            sig_names[s.name] = sig_names.get(s.name, 0) + 1
        for s in k.signals:
            if sig_names[s.name] > 1:
                continue
            rname, cname = emit_signal(out, k, s)
            registrations.append(
                f'  rb_define_method(cls_{k.name}.rb_class, "{rname}", RUBY_METHOD_FUNC({cname}), 0);')

    # Init function
    out.append('extern "C" void Init_qt6() {')
    out.append("  VALUE mQt = qt6rb::module_qt();")
    out.append("  qt6rb::init_core(mQt);")
    # Register classes parents-first
    emitted = set()
    def register(k):
        if k.name in emitted:
            return
        for b in k.bases:
            register(classes[b])
        rb_name = k.name[1:] if k.name.startswith("Q") else k.name
        super_expr = f"cls_{k.bases[0]}.rb_class" if k.bases else "Qnil"
        out.append(f'  qt6rb::define_class(&cls_{k.name}, "{rb_name}", {super_expr});')
        out.append(f"  rb_undef_alloc_func(cls_{k.name}.rb_class);")
        emitted.add(k.name)
    for k in classes.values():
        register(k)
    out.extend(registrations)
    # Enum constants
    for k in classes.values():
        for e in k.enums:
            for c in e.get_children():
                if c.kind == CursorKind.ENUM_CONSTANT_DECL:
                    out.append(f'  rb_define_const(cls_{k.name}.rb_class, "{const_name(c.spelling)}", INT2NUM({c.enum_value}));')
    out.append("}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qt-prefix", type=Path, required=True)
    ap.add_argument("--modules", nargs="+", default=["QtCore"])
    ap.add_argument("--classes", nargs="+", required=True)
    ap.add_argument("--libclang")
    ap.add_argument("-o", "--output", type=Path, default=Path("qt6_generated.cpp"))
    opts = ap.parse_args()

    libclang = opts.libclang or find_libclang()
    if libclang:
        cindex.Config.set_library_file(libclang)

    tu = parse_translation_unit(opts.qt_prefix, opts.modules)
    classes = harvest(tu, opts.qt_prefix, set(opts.classes))
    missing = set(opts.classes) - set(classes)
    if missing:
        print(f"warning: classes not found: {sorted(missing)}", file=sys.stderr)

    opts.output.write_text(generate(classes, opts.modules))
    for k in classes.values():
        print(f"{k.name}: {len(k.ctors)} ctors, {len(k.methods)} methods, "
              f"{len(k.signals)} signals, {k.skipped} skipped")
    print(f"-> {opts.output}")


if __name__ == "__main__":
    main()
