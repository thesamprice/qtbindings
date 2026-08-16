#!/usr/bin/env python3
"""Generate C++ Ruby-extension glue for Qt classes, straight off the clang AST.

Consumes cursors from parse_qt.parse_translation_unit (no intermediate IR).
Emits one .cpp file registering each requested class under the Ruby `Qt`
module: constructors, methods (snake_cased, overloads dispatched by arity and
runtime type checks), enums as constants, and signals as `on_<signal> { }`
methods implemented with compile-time member-pointer QObject::connect — no
moc involved.

Base classes of requested classes are pulled in automatically so inheritance
chains (QLabel < QFrame < QWidget < QObject) stay intact.

Marshalling: bool/ints/floats/enums, QFlags<> (as Integer), QString,
QByteArray, QStringList (Array), QVariant (native Ruby values), pointers to
generated QObject classes, and by-value/const& use of generated value classes
(QSize, QPoint, ...).

Usage:
    python3 codegen.py --qt-prefix /opt/homebrew/opt/qt \
        --modules QtCore QtGui QtWidgets \
        --classes QObject QTimer QWidget QLabel QPushButton \
        --namespace-enums -o qt6_generated.cpp
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

# Guard expressions per type kind for overload dispatch ({0} = VALUE expr).
# 'variant' accepts anything and must sort last among candidates.
GUARDS = {
    "bool": "(({0}) == Qtrue || ({0}) == Qfalse)",
    "int": "RB_INTEGER_TYPE_P({0})",
    "enum": "RB_INTEGER_TYPE_P({0})",
    "flags": "RB_INTEGER_TYPE_P({0})",
    "float": "(RB_FLOAT_TYPE_P({0}) || RB_INTEGER_TYPE_P({0}))",
    "qstring": "RB_TYPE_P({0}, T_STRING)",
    "qbytearray": "RB_TYPE_P({0}, T_STRING)",
    "cstr": "RB_TYPE_P({0}, T_STRING)",
    "qstringlist": "RB_TYPE_P({0}, T_ARRAY)",
    "variant": "1",
}


def snake(name):
    s = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s)
    return s.lower()


class Type:
    """Classification of a clang type for marshalling purposes."""
    def __init__(self, kind, cxx, to_cxx=None, to_rb=None, cls=None):
        self.kind = kind      # see GUARDS + void|objptr|objval|unsupported
        self.cxx = cxx        # C++ type spelling for lambda params/locals
        self.to_cxx = to_cxx  # fmt string, {} = VALUE expr
        self.to_rb = to_rb    # fmt string, {} = C++ expr
        self.cls = cls        # class name for objptr/objval

    def guard(self, expr):
        if self.kind == "objptr":
            return f"(NIL_P({expr}) || rb_obj_is_kind_of({expr}, cls_{self.cls}.rb_class))"
        if self.kind == "objval":
            return f"rb_obj_is_kind_of({expr}, cls_{self.cls}.rb_class)"
        return GUARDS[self.kind].format(expr)


def classify(t, generated):
    """generated: dict class name -> is_qobject (bool)"""
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

    if canon.kind == TypeKind.POINTER:
        pointee = canon.get_pointee()
        pspell = pointee.spelling.replace("const ", "").strip()
        if pspell == "char" and pointee.is_const_qualified():
            return Type("cstr", "const char*",
                        "StringValueCStr({})", "rb_str_new_cstr({})")
        if pspell in generated and generated[pspell]:
            return Type("objptr", f"{pspell}*",
                        "static_cast<%s*>(qt6rb::unwrap({}, &cls_%s))" % (pspell, pspell),
                        "qt6rb::wrap_qobject(({}), &cls_%s)" % pspell,
                        cls=pspell)
        return Type("unsupported", spelling)

    # Strip const-reference down to the value type
    value = canon
    if canon.kind == TypeKind.LVALUEREFERENCE:
        value = canon.get_pointee()
        if not value.is_const_qualified():
            return Type("unsupported", spelling)  # out-params unsupported
        value = value.get_canonical()
    vspell = value.spelling.replace("const ", "").strip()

    if vspell == "QString":
        return Type("qstring", "QString",
                    "qt6rb::to_qstring({})", "qt6rb::from_qstring({})")
    if vspell == "QByteArray":
        return Type("qbytearray", "QByteArray",
                    "qt6rb::to_qbytearray({})", "qt6rb::from_qbytearray({})")
    if vspell in ("QStringList", "QList<QString>"):
        return Type("qstringlist", "QStringList",
                    "qt6rb::to_qstringlist({})", "qt6rb::from_qstringlist({})")
    if vspell == "QVariant":
        return Type("variant", "QVariant",
                    "qt6rb::to_qvariant({})", "qt6rb::from_qvariant({})")
    if vspell.startswith("QFlags<"):
        return Type("flags", vspell,
                    "%s::fromInt(NUM2INT({}))" % vspell,
                    "INT2NUM(({}).toInt())")
    # By-value / const& use of a generated value class (QSize, QPoint, ...)
    if vspell in generated and not generated[vspell]:
        return Type("objval", vspell,
                    "*static_cast<%s*>(qt6rb::unwrap_ref({}, &cls_%s))" % (vspell, vspell),
                    "qt6rb::wrap(new %s({}), &cls_%s, true)" % (vspell, vspell),
                    cls=vspell)
    return Type("unsupported", spelling)


class Method:
    def __init__(self, cursor, generated):
        self.cursor = cursor
        self.name = cursor.spelling
        self.static = (cursor.is_static_method()
                       if cursor.kind == CursorKind.CXX_METHOD else False)
        self.result = (classify(cursor.result_type, generated)
                       if cursor.kind == CursorKind.CXX_METHOD else None)
        args = [a for a in cursor.get_arguments()
                if not a.type.spelling.endswith("QPrivateSignal")]
        self.params = [classify(a.type, generated) for a in args]
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
        self.methods = []
        self.signals = []
        self.ctors = []
        self.enums = []
        self.skipped = 0
        self.dtor_public = True
        self.abstract = False
        self.is_qobject = False


def base_cursors(cursor):
    for child in cursor.get_children():
        if child.kind == CursorKind.CXX_BASE_SPECIFIER:
            decl = child.type.get_declaration()
            if decl is not None and decl.kind in (CursorKind.CLASS_DECL,
                                                  CursorKind.STRUCT_DECL):
                yield decl


def harvest(tu, qt_prefix, wanted):
    # Index definitions of all Qt classes (top level or in namespaces)
    index = {}

    def visit(cursor):
        for child in cursor.get_children():
            if child.kind == CursorKind.NAMESPACE:
                visit(child)
            elif child.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL):
                if child.is_definition() and in_qt_headers(child, qt_prefix) \
                        and child.spelling not in index:
                    index[child.spelling] = child

    visit(tu.cursor)

    classes = {}
    queue = list(wanted)
    missing = []
    while queue:
        name = queue.pop(0)
        if name in classes:
            continue
        cursor = index.get(name)
        if cursor is None:
            missing.append(name)
            continue
        k = Klass(cursor)
        classes[name] = k
        # Pull in base classes so the inheritance chain is complete
        for base in base_cursors(cursor):
            if in_qt_headers(base, qt_prefix):
                k.bases.append(base.spelling)
                queue.append(base.spelling)

    # QObject-ness, transitively
    def qobjectish(name, seen=None):
        if name == "QObject":
            return True
        seen = seen or set()
        if name in seen or name not in classes:
            return False
        seen.add(name)
        return any(qobjectish(b, seen) for b in classes[name].bases)

    generated = {}
    for name, k in classes.items():
        k.is_qobject = qobjectish(name)
        generated[name] = k.is_qobject

    for k in classes.values():
        for child in k.cursor.get_children():
            if child.kind == CursorKind.CXX_METHOD:
                # Purity matters at any access level for abstractness
                if child.is_pure_virtual_method():
                    k.abstract = True
                    continue
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method():
                    continue
                if child.spelling.startswith("operator"):
                    continue
                ann = collect_annotations(child)
                m = Method(child, generated)
                if not m.supported:
                    k.skipped += 1
                elif "qt_signal" in ann:
                    k.signals.append(m)
                else:
                    k.methods.append(m)
            elif child.kind == CursorKind.CONSTRUCTOR:
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method() or child.is_copy_constructor() \
                        or child.is_move_constructor():
                    continue
                m = Method(child, generated)
                if m.supported:
                    k.ctors.append(m)
                else:
                    k.skipped += 1
            elif child.kind == CursorKind.DESTRUCTOR:
                k.dtor_public = child.access_specifier == AccessSpecifier.PUBLIC
            elif child.kind == CursorKind.ENUM_DECL and child.spelling:
                if child.access_specifier == AccessSpecifier.PUBLIC:
                    k.enums.append(child)
    return classes, missing


def harvest_namespace_enums(tu, qt_prefix, namespace="Qt"):
    """Constants from `namespace Qt` enums (AlignCenter, Horizontal, ...)."""
    constants = {}
    for child in tu.cursor.get_children():
        if child.kind != CursorKind.NAMESPACE or child.spelling != namespace:
            continue
        if not in_qt_headers(child, qt_prefix):
            continue
        for enum in child.get_children():
            if enum.kind != CursorKind.ENUM_DECL:
                continue
            for c in enum.get_children():
                if c.kind == CursorKind.ENUM_CONSTANT_DECL:
                    name = const_name(c.spelling)
                    if re.match(r'^[A-Z][A-Za-z0-9_]*$', name) \
                            and name not in constants:
                        constants[name] = c.enum_value
    return constants


def const_name(name):
    return name[0].upper() + name[1:] if name else name


def emit_overload(out, m, klass, n, indent):
    """Emit the call for overload m invoked with n args."""
    args = ", ".join(m.params[i].to_cxx.format(f"argv[{i}]") for i in range(n))
    if m.cursor.kind == CursorKind.CONSTRUCTOR:
        call = f"new {klass.name}({args})"
        if klass.is_qobject:
            out.append(f"{indent}return qt6rb::wrap_qobject({call}, &cls_{klass.name});")
        else:
            out.append(f"{indent}return qt6rb::wrap({call}, &cls_{klass.name}, true);")
        return
    if m.static:
        call = f"{klass.name}::{m.name}({args})"
    else:
        call = f"o->{m.name}({args})"
    if m.result.kind == "void":
        out.append(f"{indent}{call};")
        out.append(f"{indent}return Qnil;")
    else:
        out.append(f"{indent}return {m.result.to_rb.format(call)};")


def emit_method_group(out, klass, ruby_name, overloads, static):
    """One C function dispatching same-named overloads by arity + type."""
    cname = f"rb_{klass.name}_{'s_' if static else ''}{ruby_name}"
    out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
    if not static:
        out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    out.append("  (void)argv; (void)self;")

    arities = sorted({n for m in overloads
                      for n in range(m.min_args, len(m.params) + 1)})
    for n in arities:
        candidates = [m for m in overloads if m.min_args <= n <= len(m.params)]
        # Most-specific first: overloads with catch-all variant params last
        candidates.sort(key=lambda m: sum(1 for p in m.params[:n]
                                          if p.kind == "variant"))
        out.append(f"  if (argc == {n}) {{")
        if len(candidates) == 1:
            emit_overload(out, candidates[0], klass, n, "    ")
        else:
            for m in candidates:
                guards = " && ".join(m.params[i].guard(f"argv[{i}]")
                                     for i in range(n)) or "1"
                out.append(f"    if ({guards}) {{")
                emit_overload(out, m, klass, n, "      ")
                out.append("    }")
            out.append(f'    rb_raise(rb_eTypeError, "no matching overload of {klass.name}#{ruby_name} for given argument types");')
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


def generate(classes, modules, ns_constants):
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
        qobj = "true" if k.is_qobject else "false"
        out.append(f'static qt6rb::ClassInfo cls_{k.name} = {{ "{k.name}", Qnil, {deleter}, {qobj} }};')
    out.append("")

    registrations = []
    for k in classes.values():
        if k.ctors and not k.abstract:
            cname = emit_method_group(out, k, "new", k.ctors, static=True)
            registrations.append(
                f'  rb_define_singleton_method(cls_{k.name}.rb_class, "new", RUBY_METHOD_FUNC({cname}), -1);')
        for static in (False, True):
            groups = {}
            for m in k.methods:
                if m.static != static:
                    continue
                groups.setdefault(snake(m.name), []).append(m)
            for rname, overloads in groups.items():
                cname = emit_method_group(out, k, rname, overloads, static)
                target = f"cls_{k.name}.rb_class"
                define = "rb_define_singleton_method" if static else "rb_define_method"
                registrations.append(
                    f'  {define}({target}, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')
                if not static:
                    # setter sugar: set_interval(v) also as interval=
                    if rname.startswith("set_") and any(len(m.params) >= 1 for m in overloads):
                        registrations.append(
                            f'  rb_define_alias({target}, "{rname[4:]}=", "{rname}");')
                    # predicate sugar: is_active also as active?
                    if rname.startswith("is_") and all(not m.params for m in overloads) \
                            and all(m.result.kind == "bool" for m in overloads):
                        registrations.append(
                            f'  rb_define_alias({target}, "{rname[3:]}?", "{rname}");')
        # Signals (skip overloaded ones -- member pointer would be ambiguous)
        sig_counts = {}
        for s in k.signals:
            sig_counts[s.name] = sig_counts.get(s.name, 0) + 1
        for s in k.signals:
            if sig_counts[s.name] > 1:
                continue
            rname, cname = emit_signal(out, k, s)
            registrations.append(
                f'  rb_define_method(cls_{k.name}.rb_class, "{rname}", RUBY_METHOD_FUNC({cname}), 0);')

    out.append('extern "C" void Init_qt6() {')
    out.append("  VALUE mQt = qt6rb::module_qt();")
    out.append("  qt6rb::init_core(mQt);")
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
    for k in classes.values():
        for e in k.enums:
            for c in e.get_children():
                if c.kind == CursorKind.ENUM_CONSTANT_DECL:
                    name = const_name(c.spelling)
                    if re.match(r'^[A-Z][A-Za-z0-9_]*$', name):
                        out.append(f'  rb_define_const(cls_{k.name}.rb_class, "{name}", INT2NUM({c.enum_value}));')
    for name, value in ns_constants.items():
        # Class names (Widget, Window, Dialog, ...) win over enum values
        out.append(f'  if (!rb_const_defined(mQt, rb_intern("{name}"))) '
                   f'rb_define_const(mQt, "{name}", INT2NUM({value}));')
    out.append("}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qt-prefix", type=Path, required=True)
    ap.add_argument("--modules", nargs="+", default=["QtCore"])
    ap.add_argument("--classes", nargs="+", required=True)
    ap.add_argument("--namespace-enums", action="store_true",
                    help="Emit Qt namespace enum values as Qt:: constants")
    ap.add_argument("--libclang")
    ap.add_argument("-o", "--output", type=Path, default=Path("qt6_generated.cpp"))
    opts = ap.parse_args()

    libclang = opts.libclang or find_libclang()
    if libclang:
        cindex.Config.set_library_file(libclang)

    tu = parse_translation_unit(opts.qt_prefix, opts.modules)
    classes, missing = harvest(tu, opts.qt_prefix, list(opts.classes))
    if missing:
        print(f"warning: classes not found: {sorted(set(missing))}", file=sys.stderr)
    ns_constants = harvest_namespace_enums(tu, opts.qt_prefix) \
        if opts.namespace_enums else {}

    opts.output.write_text(generate(classes, opts.modules, ns_constants))
    for k in classes.values():
        print(f"{k.name}: {len(k.ctors)} ctors, {len(k.methods)} methods, "
              f"{len(k.signals)} signals, {k.skipped} skipped"
              f"{' (QObject)' if k.is_qobject else ''}")
    if ns_constants:
        print(f"Qt namespace constants: {len(ns_constants)}")
    print(f"-> {opts.output}")


if __name__ == "__main__":
    main()
