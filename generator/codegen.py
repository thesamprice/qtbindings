#!/usr/bin/env python3
"""Generate C++ Ruby-extension glue for Qt classes, straight off the clang AST.

Consumes cursors from parse_qt.parse_translation_unit (no intermediate IR).
Emits one .cpp file registering each requested class under the Ruby `Qt`
module: constructors, methods (snake_cased, overloads dispatched by arity and
runtime type checks), enums as constants, and signals as `on_<signal> { }`
methods implemented with compile-time member-pointer QObject::connect — no
moc involved.

Virtual override hooks: each instantiable class gets a C++ shim subclass
(Rb_QWidget : QWidget) overriding every supported virtual (public and
protected, collected transitively from base classes). Overrides dispatch to
the Ruby method (snake_cased) when the instance's Ruby class defines one,
else call the base implementation. Construction goes through allocate +
#initialize so idiomatic Ruby subclassing works:

    class MyWidget < Qt::Widget
      def initialize(parent = nil)
        super(parent)          # constructs the C++ object
      end
      def size_hint            # called from C++ layout machinery
        Qt::Size.new(123, 45)
      end
      def close_event(event)   # protected virtual hook
        event.accept
        super(event)           # calls QWidget::closeEvent
      end
    end

Base classes of requested classes are pulled in automatically so inheritance
chains (QLabel < QFrame < QWidget < QObject) stay intact.

Usage:
    python3 codegen.py --qt-prefix /opt/homebrew/opt/qt \
        --modules QtCore QtGui QtWidgets \
        --classes QObject QTimer QWidget QLabel QPushButton \
        --namespace-enums -o qt6_generated.cpp
"""

import argparse
import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_qt import (cindex, CursorKind, AccessSpecifier, TypeKind,
                      parse_translation_unit, find_libclang, in_qt_headers,
                      collect_annotations)


# Inheritance depth per generated class, filled in by harvest(). Used to order
# overload guards most-derived-first (see param_specificity).
CLASS_DEPTH = {}


def mangle(cxx_name):
    """Nested types (QTextEdit::ExtraSelection) are keyed and emitted under a
    flattened name. The generated file aliases it back to the real type
    (`using QTextEdit__ExtraSelection = QTextEdit::ExtraSelection;`) so the
    same string is valid both as a C++ type and as a C identifier suffix,
    which is what every cls_/rb_ symbol below is built from."""
    return cxx_name.replace("::", "__")

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

# Narrow character types need an explicit narrowing cast rather than a bare
# NUM2* macro, so they carry full conversion expressions instead of the
# macro-name pairs in INT_TYPES. QChar is the motivating case: as of Qt 6.9
# every integral QChar constructor is compiled out (QT_CORE_REMOVED_SINCE)
# and the only remaining entry points -- QChar::fromUcs2/fromUcs4 and the
# QChar(uchar, uchar) constructor -- are spelled in these types.
CHAR_TYPES = {
    "char16_t": ("static_cast<char16_t>(NUM2UINT({}))",
                 "UINT2NUM(static_cast<unsigned int>({}))"),
    "char32_t": ("static_cast<char32_t>(NUM2UINT({}))",
                 "UINT2NUM(static_cast<unsigned int>({}))"),
    "unsigned char": ("static_cast<unsigned char>(NUM2UINT({}))",
                      "UINT2NUM(static_cast<unsigned int>({}))"),
    "signed char": ("static_cast<signed char>(NUM2INT({}))",
                    "INT2NUM(static_cast<int>({}))"),
}

# Element types accepted inside QList<...>, i.e. those with a
# qt6rb::num_to/num_from overload. char is deliberately absent: QList<char> is
# a byte buffer in practice and QByteArray already covers that.
NUMLIST_TYPES = {
    "int", "unsigned int", "short", "unsigned short", "long", "unsigned long",
    "long long", "unsigned long long", "float", "double",
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
    "numlist": "RB_TYPE_P({0}, T_ARRAY)",
    "objvallist": "RB_TYPE_P({0}, T_ARRAY)",
    "objptrlist": "RB_TYPE_P({0}, T_ARRAY)",
    "variant": "1",
    # `bool *ok` out-params take nil or a Qt::Boolean; anything goes
    "boolout": "1",
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
        # qt6rb::is_kind_of also accepts C++ bases the single-inheritance
        # Ruby hierarchy cannot express (QWidget is also a QPaintDevice)
        if self.kind == "objptr":
            return f"(NIL_P({expr}) || qt6rb::is_kind_of({expr}, &cls_{self.cls}))"
        if self.kind == "objval":
            return f"qt6rb::is_kind_of({expr}, &cls_{self.cls})"
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
    if spelling in CHAR_TYPES:
        n2, i2 = CHAR_TYPES[spelling]
        return Type("int", spelling, n2, i2)
    if canon.kind in (TypeKind.DOUBLE, TypeKind.FLOAT):
        return Type("float", spelling, "NUM2DBL({})", "DBL2NUM({})")
    if canon.kind == TypeKind.ENUM:
        return Type("enum", spelling,
                    "static_cast<%s>(NUM2INT({}))" % spelling,
                    "INT2NUM(static_cast<int>({}))")

    if canon.kind == TypeKind.POINTER:
        pointee = canon.get_pointee()
        pspell = mangle(pointee.spelling.replace("const ", "").strip())
        if pspell == "char" and pointee.is_const_qualified():
            # The static_cast matters: StringValueCStr yields char*, and an
            # unqualified char* makes C++ prefer Qt's functor template
            # overloads (QShortcut's constructors) over the const char* ones
            return Type("cstr", "const char*",
                        "static_cast<const char*>(StringValueCStr({}))",
                        "rb_str_new_cstr({})")
        if pspell == "bool" and not pointee.is_const_qualified():
            # Qt's `bool *ok` out-param idiom (QInputDialog::getText, ...).
            # qt6rb::BoolOut is a temporary that converts to bool* and, when
            # it dies at the end of the enclosing full expression (i.e. after
            # the wrapped call returns), writes the flag back into the Ruby
            # object via value=. nil is accepted and simply discards it.
            return Type("boolout", "bool*", "qt6rb::BoolOut({})")
        if pspell in generated:
            if generated[pspell]:  # QObject-derived
                return Type("objptr", f"{pspell}*",
                            "static_cast<%s*>(qt6rb::unwrap_release({}, &cls_%s))" % (pspell, pspell),
                            "qt6rb::wrap_qobject((QObject*)({}), &cls_%s)" % pspell,
                            cls=pspell)
            # Polymorphic non-QObject (events, items): passing by pointer
            # transfers ownership to Qt (tree/table/list containers)
            return Type("objptr", f"{pspell}*",
                        "static_cast<%s*>(qt6rb::unwrap_release({}, &cls_%s))" % (pspell, pspell),
                        "qt6rb::wrap((void*)({}), &cls_%s, false)" % pspell,
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
    if vspell in ("QAnyStringView", "QStringView"):
        # Views over a temporary QString are valid for the full expression,
        # which is exactly the lifetime of the bound call
        return Type("qstring", "QString",
                    vspell + "(qt6rb::to_qstring({}))",
                    "qt6rb::from_qstring(({}).toString())")
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
    # QList of a generated class <-> Ruby Array. A list of values
    # (QTextEdit::setExtraSelections) copies each element, matching Qt's
    # by-value container semantics; a list of pointers (QWidget::actions,
    # QMenu::addActions) wraps them the same way a bare pointer would.
    if vspell.startswith("QList<") and vspell.endswith(">"):
        inner = vspell[len("QList<"):-1].strip()
        # QList of a primitive numeric type <-> Ruby Array of Numerics
        # (QSplitter::setSizes/sizes). qt6rb::num_to/num_from give each
        # element type its own NUM2* macro, so elements range-check the same
        # way a bare scalar parameter of that type does.
        if inner in NUMLIST_TYPES:
            return Type("numlist", f"QList<{inner}>",
                        "qt6rb::to_numlist<%s>({})" % inner,
                        "qt6rb::from_numlist<%s>({})" % inner)
        if inner.endswith("*"):
            elem = mangle(inner[:-1].strip())
            if elem in generated:
                from_fn = "from_qobjptrlist" if generated[elem] else "from_objptrlist"
                return Type("objptrlist", f"QList<{elem}*>",
                            "qt6rb::to_objptrlist<%s>({}, &cls_%s)" % (elem, elem),
                            "qt6rb::%s<%s>({}, &cls_%s)" % (from_fn, elem, elem),
                            cls=elem)
        else:
            elem = mangle(inner)
            if elem in generated and not generated[elem]:
                return Type("objvallist", f"QList<{elem}>",
                            "qt6rb::to_objlist<%s>({}, &cls_%s)" % (elem, elem),
                            "qt6rb::from_objlist<%s>({}, &cls_%s)" % (elem, elem),
                            cls=elem)
    # By-value / const& use of a generated value class (QSize, QPoint, ...)
    vspell = mangle(vspell)
    if vspell in generated and not generated[vspell]:
        return Type("objval", vspell,
                    "*static_cast<%s*>(qt6rb::unwrap_ref({}, &cls_%s))" % (vspell, vspell),
                    "qt6rb::wrap(new %s({}), &cls_%s, true)" % (vspell, vspell),
                    cls=vspell)
    return Type("unsupported", spelling)


def is_moc_internal(name):
    """Q_OBJECT/Q_GADGET plumbing, never part of the public API.

    qt_metacall/qt_metacast/qt_static_metacall are moc's dispatch entry
    points, and qt_check_for_QGADGET_macro is declared but never defined --
    it exists only as a compile-time marker, so binding it leaves an
    undefined symbol in the extension. No public Qt API starts with qt_."""
    return name.startswith("qt_")


def is_final(cursor):
    return any(c.kind == CursorKind.CXX_FINAL_ATTR for c in cursor.get_children())


class Method:
    def __init__(self, cursor, generated):
        self.cursor = cursor
        self.name = cursor.spelling
        is_cxx = cursor.kind == CursorKind.CXX_METHOD
        parent = cursor.semantic_parent
        self.owner = parent.spelling if parent is not None else None
        self.static = cursor.is_static_method() if is_cxx else False
        self.virtual = cursor.is_virtual_method() if is_cxx else False
        self.pure = cursor.is_pure_virtual_method() if is_cxx else False
        self.const = cursor.is_const_method() if is_cxx else False
        self.access = cursor.access_specifier
        self.result = classify(cursor.result_type, generated) if is_cxx else None
        self.result_decl = cursor.result_type.spelling if is_cxx else None
        args = [a for a in cursor.get_arguments()
                if not a.type.spelling.endswith("QPrivateSignal")]
        self.params = [classify(a.type, generated) for a in args]
        self.param_decls = [a.type.spelling for a in args]
        self.min_args = 0
        for a in args:
            tokens = [t.spelling for t in a.get_tokens()]
            if "=" in tokens:
                break
            self.min_args += 1
        self.dispatch_min = self.min_args
        # Optional trailing params of unsupported types don't block the
        # method: call with the supported prefix, C++ defaults fill the rest
        self.truncated = False
        first_bad = next((i for i, pt in enumerate(self.params)
                          if pt.kind in ("unsupported", "void")), None)
        if first_bad is not None and first_bad >= self.min_args:
            self.params = self.params[:first_bad]
            self.param_decls = self.param_decls[:first_bad]
            self.truncated = True
            first_bad = None
        self.supported = first_bad is None \
            and (self.result is None
                 or self.result.kind not in ("unsupported", "boolout"))


class Klass:
    def __init__(self, cursor, cxx=None):
        self.cursor = cursor
        # cxx is the real C++ spelling; name is the flattened symbol-safe key
        # (identical for top-level classes)
        self.cxx = cxx or cursor.spelling
        self.name = mangle(self.cxx)
        # Enclosing class for a nested type, so the Ruby class can be defined
        # under it (Qt::TextEdit::ExtraSelection) rather than under Qt
        self.outer = mangle(self.cxx.rsplit("::", 1)[0]) if "::" in self.cxx else None
        self.bases = []
        self.methods = []
        self.prot_methods = []  # protected non-virtual, own + inherited
        self.signals = []
        self.ctors = []
        self.fields = []        # public data members (name, Type, writable)
        self.enums = []
        self.virtuals = []      # hookable virtuals (own + inherited)
        self.shim = False
        self.saw_ctor = False      # any constructor cursor at all, even non-public
        self.implicit_ctor = False # plain struct relying on the implicit default ctor
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


def publicly_typed(cursor):
    """False when a param or the return type names a protected/private
    nested type (e.g. QAbstractSlider::SliderChange) which free binding
    functions cannot reference."""
    def ok(t):
        decl = t.get_canonical().get_declaration()
        if decl is None or decl.kind == CursorKind.NO_DECL_FOUND:
            return True
        return decl.access_specifier not in (AccessSpecifier.PROTECTED,
                                             AccessSpecifier.PRIVATE)
    if not ok(cursor.result_type):
        return False
    return all(ok(a.type) for a in cursor.get_arguments())


def collect_virtuals(klass, classes, generated):
    """Hookable virtuals for klass: own + inherited, nearest declaration
    wins; a nearest-but-unhookable declaration blocks the signature.

    Pure virtuals are hookable too: the shim implements them by dispatching to
    Ruby, which is what lets a Ruby subclass of an abstract Qt class
    (QSyntaxHighlighter, QValidator) be instantiated at all. Returns
    (methods, all_pure_hooked); when a pure virtual could not be hooked the
    shim would still be abstract, so the class stays unconstructible."""
    sigs = {}
    pure_keys = set()

    def visit(name):
        k = classes.get(name)
        if k is None:
            return
        for child in k.cursor.get_children():
            if child.kind != CursorKind.CXX_METHOD:
                continue
            if not child.is_virtual_method():
                continue
            if child.spelling.startswith("operator"):
                continue
            if is_moc_internal(child.spelling):
                continue
            key = (child.spelling,
                   tuple(a.type.get_canonical().spelling
                         for a in child.get_arguments()))
            if key in sigs:
                continue
            if child.access_specifier == AccessSpecifier.PRIVATE:
                # A private redeclaration hides the inherited virtual
                sigs[key] = None
                continue
            if child.is_pure_virtual_method():
                pure_keys.add(key)
            elif is_final(child):
                sigs[key] = None
                continue
            if collect_annotations(child) or not publicly_typed(child):
                sigs[key] = None
                continue
            m = Method(child, generated)
            # Truncated signatures can't be used as overrides (must match),
            # and boolout params have no Ruby representation to pass back
            usable = m.supported and not m.truncated \
                and all(p.kind != "boolout" for p in m.params)
            sigs[key] = m if usable else None
        for b in k.bases:
            visit(b)

    visit(klass.name)
    all_pure_hooked = all(sigs.get(key) for key in pure_keys)
    return [m for m in sigs.values() if m], all_pure_hooked


def collect_protected(klass, classes, generated):
    """Protected non-virtual methods, own + inherited. These are part of the
    subclassing API (QPlainTextEdit::firstVisibleBlock,
    QAbstractScrollArea::setViewportMargins) and qtbindings-era code calls
    them from Ruby subclasses. The shim re-exposes them with a `using`
    declaration, so they are reachable only on Ruby-created instances --
    which is exactly the C++ rule. Protected virtuals are excluded: those go
    through collect_virtuals/qt6rb_base_ so `super` reaches the base
    implementation instead of recursing into the Ruby override."""
    found, public_names = {}, set()

    def visit(name):
        k = classes.get(name)
        if k is None:
            return
        for child in k.cursor.get_children():
            if child.kind != CursorKind.CXX_METHOD:
                continue
            if child.spelling.startswith("operator") or child.is_deleted_method():
                continue
            if is_moc_internal(child.spelling):
                continue
            if child.access_specifier == AccessSpecifier.PUBLIC:
                public_names.add(child.spelling)
                continue
            if child.access_specifier != AccessSpecifier.PROTECTED:
                continue
            if child.is_virtual_method() or child.is_static_method():
                continue
            if collect_annotations(child):  # signals/slots
                continue
            # e.g. QAbstractItemView::state() returns the protected enum
            # State, which a free binding function cannot name
            if not publicly_typed(child):
                continue
            m = Method(child, generated)
            if m.supported and not m.truncated:
                found.setdefault(child.spelling, []).append(m)

        for b in k.bases:
            visit(b)

    visit(klass.name)
    # A public overload of the same name wins; Ruby has one method per name
    return [m for name, ms in found.items() if name not in public_names
            for m in ms]


APP_CLASSES = ("QCoreApplication", "QGuiApplication", "QApplication")


def harvest(tu, qt_prefix, wanted):
    # Index definitions of all Qt classes (top level or in namespaces)
    index = {}

    def visit(cursor, prefix=""):
        for child in cursor.get_children():
            if child.kind == CursorKind.NAMESPACE:
                visit(child)
            elif child.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL):
                if not (child.is_definition() and in_qt_headers(child, qt_prefix)):
                    continue
                qname = prefix + child.spelling
                if qname not in index:
                    index[qname] = child
                # Public nested types (QTextEdit::ExtraSelection) are reachable
                # only by their qualified name, which is how they are requested
                if child.spelling.startswith("Q"):
                    visit(child, qname + "::")

    visit(tu.cursor)

    # classes is keyed by the flattened symbol-safe name throughout; the queue
    # carries real C++ spellings because that is what the index and the
    # --classes arguments use
    classes = {}
    queue = list(wanted)
    missing = []
    while queue:
        cxx = queue.pop(0)
        if mangle(cxx) in classes:
            continue
        cursor = index.get(cxx)
        if cursor is None:
            missing.append(cxx)
            continue
        k = Klass(cursor, cxx)
        k.abstract = bool(cursor.is_abstract_record())
        classes[k.name] = k
        # Pull in base classes so the inheritance chain is complete
        for base in base_cursors(cursor):
            if in_qt_headers(base, qt_prefix):
                k.bases.append(mangle(base.spelling))
                queue.append(base.spelling)
        # A nested type's enclosing class must exist to define it under
        if k.outer:
            queue.append(cxx.rsplit("::", 1)[0])

    # Drop bases that didn't resolve to a generated class (e.g. template
    # bases like QList<QPoint> under QPolygon)
    for k in classes.values():
        k.bases = [b for b in k.bases if b in classes]

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

    def depth(name, seen=None):
        k = classes.get(name)
        if k is None or not k.bases:
            return 0
        seen = seen or set()
        if name in seen:
            return 0
        seen.add(name)
        return 1 + max(depth(b, seen) for b in k.bases)

    CLASS_DEPTH.clear()
    for name in classes:
        CLASS_DEPTH[name] = depth(name)

    for k in classes.values():
        for child in k.cursor.get_children():
            if child.kind == CursorKind.FIELD_DECL:
                # Public data members: the QStyleOption family is configured
                # entirely through them (opt.rect =, opt.text =)
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                ft = classify(child.type, generated)
                if ft.kind in ("unsupported", "void", "boolout"):
                    k.skipped += 1
                    continue
                writable = not child.type.is_const_qualified()
                k.fields.append((child.spelling, ft, writable))
            elif child.kind == CursorKind.CXX_METHOD:
                # Pure virtuals are still callable through the abstract base
                # (QStyle::drawControl); only overriding them is impossible,
                # and collect_virtuals filters those separately.
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method():
                    continue
                if child.spelling.startswith("operator"):
                    continue
                if is_moc_internal(child.spelling):
                    continue
                ann = collect_annotations(child)
                m = Method(child, generated)
                if not m.supported:
                    k.skipped += 1
                elif "qt_signal" in ann:
                    if all(p.kind != "boolout" for p in m.params):
                        k.signals.append(m)
                    else:
                        k.skipped += 1
                else:
                    k.methods.append(m)
            elif child.kind == CursorKind.CONSTRUCTOR:
                k.saw_ctor = True
                if child.access_specifier != AccessSpecifier.PUBLIC:
                    continue
                if child.is_deleted_method() or child.is_move_constructor():
                    continue
                # Copy constructors are useful for value classes
                # (Qt::StyleOptionViewItem.new(other)); a QObject's copy ctor
                # is deleted anyway, but skip them for clarity
                if child.is_copy_constructor() and generated.get(k.name):
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

    for k in classes.values():
        # A struct that declares no constructor at all (QTextEdit::ExtraSelection)
        # still has an implicit default one, but libclang emits no cursor for
        # it, so synthesize the zero-argument form.
        if not k.saw_ctor and not k.abstract and not k.is_qobject:
            k.implicit_ctor = True
        if k.name in APP_CLASSES:
            k.shim = False
            continue
        if k.ctors or k.implicit_ctor:
            k.virtuals, all_pure_hooked = collect_virtuals(k, classes, generated)
            k.prot_methods = collect_protected(k, classes, generated)
            k.shim = bool(k.virtuals) or bool(k.prot_methods)
            # An abstract class becomes constructible once the shim supplies a
            # Ruby-dispatching body for every pure virtual it inherits
            if k.abstract and k.virtuals and all_pure_hooked:
                k.abstract = False
            elif k.abstract:
                k.virtuals, k.prot_methods, k.shim = [], [], False
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
                    name = c.spelling
                    if name and name not in constants:
                        constants[name] = c.enum_value
    return constants


def const_name(name):
    return name[0].upper() + name[1:] if name else name


def param_specificity(m, n):
    """How derived an overload's object parameters are. Guards are plain
    is_kind_of checks, so an overload taking a base class matches arguments
    meant for a more derived sibling -- QSyntaxHighlighter(QObject*) would
    swallow the QTextDocument* call and leave the highlighter attached to no
    document. Testing the deepest class first restores C++ overload
    resolution."""
    return sum(CLASS_DEPTH.get(p.cls, 0) for p in m.params[:n] if p.cls)


def emit_dispatch(out, overloads, error_name, body, fallback=None):
    """Shared arity + type-guard dispatcher. body(m, n, indent) emits the
    per-overload code. fallback replaces the final arity error."""
    arities = sorted({n for m in overloads
                      for n in range(m.dispatch_min, len(m.params) + 1)})
    for n in arities:
        candidates = [m for m in overloads
                      if m.dispatch_min <= n <= len(m.params)]
        # Most-specific first: overloads with catch-all variant params last,
        # and among the rest the most derived object parameters first
        candidates.sort(key=lambda m: (sum(1 for p in m.params[:n]
                                           if p.kind == "variant"),
                                       -param_specificity(m, n)))
        out.append(f"  if (argc == {n}) {{")
        if len(candidates) == 1:
            body(candidates[0], n, "    ")
        else:
            for m in candidates:
                guards = " && ".join(m.params[i].guard(f"argv[{i}]")
                                     for i in range(n)) or "1"
                out.append(f"    if ({guards}) {{")
                body(m, n, "      ")
                out.append("    }")
            out.append(fallback or
                       f'    rb_raise(rb_eTypeError, "no matching overload of '
                       f'{error_name} for given argument types");')
        out.append("  }")
    out.append(fallback or
               f'  rb_raise(rb_eArgError, "wrong number of arguments for '
               f'{error_name} (%d)", argc);')


# C++ methods whose Ruby name shadows a core method: on arity/type mismatch
# fall through to super (e.g. widget.raise -> C++, raise Error -> Kernel)
RESERVED_FALLBACK = {"raise", "clone", "dup", "display", "format", "hash"}


def conv_args(m, n):
    return ", ".join(m.params[i].to_cxx.format(f"argv[{i}]") for i in range(n))


def emit_method_group(out, klass, ruby_name, overloads, static):
    """One C function dispatching same-named overloads by arity + type."""
    cname = f"rb_{klass.name}_{'s_' if static else ''}{ruby_name}"
    out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
    if not static:
        out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    out.append("  (void)argv; (void)self;")

    def body(m, n, indent):
        args = conv_args(m, n)
        qual = m.owner or klass.name
        if m.static:
            call = f"{qual}::{m.name}({args})"
        elif qual != klass.name and m.pure:
            # As above, but the pure virtual is inherited: QGridLayout hides
            # QLayout's pure addItem(QLayoutItem*) behind its own protected
            # override, so neither the qualified call nor the unqualified one
            # compiles. Go through the base to dispatch virtually to whatever
            # the concrete class actually provides.
            call = f"static_cast<{qual}*>(o)->{m.name}({args})"
        elif qual != klass.name:
            # Inherited overload hidden by a redeclaration in this class;
            # only reachable with explicit qualification
            call = f"o->{qual}::{m.name}({args})"
        elif m.pure and klass.shim:
            # A pure virtual has no implementation to call non-virtually. On
            # a Ruby-created object this binding is only reachable from the
            # override's own super(), which would otherwise recurse straight
            # back into the override; the shim raises for the same reason.
            # C++-created objects still dispatch virtually to the real one.
            out.append(f"{indent}if (dynamic_cast<Rb_{klass.name}*>(o)) {{")
            out.append(f'{indent}  rb_raise(rb_eNotImpError, "{klass.cxx}#{ruby_name} '
                       f'is abstract and must be overridden");')
            out.append(f"{indent}}}")
            call = f"o->{m.name}({args})"
        elif m.virtual and klass.shim:
            # For Ruby-created objects call this class's implementation
            # non-virtually so a Ruby override calling super doesn't recurse
            call = (f"(dynamic_cast<Rb_{klass.name}*>(o)"
                    f" ? o->{klass.name}::{m.name}({args})"
                    f" : o->{m.name}({args}))")
        else:
            call = f"o->{m.name}({args})"
        if m.result.kind == "void":
            out.append(f"{indent}{call};")
            out.append(f"{indent}return Qnil;")
        else:
            out.append(f"{indent}return {m.result.to_rb.format(call)};")

    if ruby_name in RESERVED_FALLBACK:
        emit_dispatch(out, overloads, f"{klass.name}#{ruby_name}", body,
                      fallback="  return rb_call_super(argc, argv);")
    else:
        emit_dispatch(out, overloads, f"{klass.name}#{ruby_name}", body)
    out.append("}")
    out.append("")
    return cname


def emit_ctor(out, klass):
    cname = f"rb_{klass.name}_ctor"
    out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
    out.append("  (void)argv;")
    if klass.name in APP_CLASSES:
        # Q*Application constructors need stable argc/argv storage
        out.append("  VALUE rb_args = Qnil;")
        out.append('  rb_scan_args(argc, argv, "01", &rb_args);')
        out.append("  int* ac; char** av;")
        out.append("  qt6rb::app_args(rb_args, &ac, &av);")
        out.append(f"  {klass.name}* p = new {klass.name}(*ac, av);")
        out.append("  qt6rb::attach(self, p, false);")
        out.append("  return self;")
    else:
        cxx = f"Rb_{klass.name}" if klass.shim else klass.name
        owned = "false" if klass.is_qobject else "true"

        def body(m, n, indent):
            out.append(f"{indent}{cxx}* p = new {cxx}({conv_args(m, n)});")
            out.append(f"{indent}qt6rb::attach(self, p, {owned});")
            if klass.shim:
                out.append(f"{indent}p->qt6rb_set_self(self);")
            out.append(f"{indent}return self;")

        if klass.implicit_ctor:
            out.append("  if (argc == 0) {")
            out.append(f"    {cxx}* p = new {cxx}();")
            out.append(f"    qt6rb::attach(self, p, {owned});")
            out.append("    return self;")
            out.append("  }")
        emit_dispatch(out, klass.ctors, f"{klass.name}#initialize", body)
    out.append("}")
    out.append(f"static VALUE rb_{klass.name}_alloc(VALUE klass) {{ return qt6rb::alloc_wrapper(klass, &cls_{klass.name}); }}")
    out.append("")
    return cname


def emit_shim(out, klass):
    shim = f"Rb_{klass.name}"
    # qt6rb::RubyPeer supplies qt6rb_self / qt6rb_set_self and unregisters the
    # GC root in its destructor. Deriving from it (rather than redeclaring the
    # members here) lets the runtime recover the Ruby object from a bare
    # QObject* via dynamic_cast, which is how wrap_qobject preserves identity.
    out.append(f"class {shim} : public {klass.name}, public qt6rb::RubyPeer {{")
    out.append("public:")
    out.append(f"  using {klass.name}::{klass.name};")
    # Inherited-constructor declarations never include the copy constructor,
    # so forward it explicitly when the class exposes one
    if any(m.cursor.is_copy_constructor() for m in klass.ctors):
        out.append(f"  {shim}(const {klass.name}& other) : {klass.name}(other) {{}}")
    # Re-expose inherited protected non-virtuals as public on the shim, so the
    # Ruby wrappers below can call them (one `using` covers all overloads)
    for owner in dict.fromkeys(m.owner for m in klass.prot_methods):
        names = sorted({m.name for m in klass.prot_methods if m.owner == owner})
        for name in names:
            out.append(f"  using {mangle(owner)}::{name};")
    # Public forwarders so protected base implementations are callable
    for m in klass.virtuals:
        if m.access != AccessSpecifier.PROTECTED or m.pure:
            continue
        params = ", ".join(f"{d} a{i}" for i, d in enumerate(m.param_decls))
        args = ", ".join(f"a{i}" for i in range(len(m.params)))
        ret = "" if m.result.kind == "void" else "return "
        out.append(f"  {m.result_decl} qt6rb_base_{m.name}({params}) {{ {ret}{klass.name}::{m.name}({args}); }}")
    # Overrides dispatching to Ruby when the user's class defines the method
    for m in klass.virtuals:
        rname = snake(m.name)
        params = ", ".join(f"{d} a{i}" for i, d in enumerate(m.param_decls))
        constq = " const" if m.const else ""
        args = ", ".join(f"a{i}" for i in range(len(m.params)))
        out.append(f"  {m.result_decl} {m.name}({params}){constq} override {{")
        out.append(f'    if (const char* rbname = qt6rb::pick_override(qt6rb_self, &cls_{klass.name}, "{rname}", "{m.name}")) {{')
        if m.params:
            conv = ", ".join(t.to_rb.format(f"a{i}") for i, t in enumerate(m.params))
            out.append(f"      VALUE rb_args[] = {{ {conv} }};")
            argse = f"{len(m.params)}, rb_args"
        else:
            argse = "0, nullptr"
        out.append("      bool ok = true;")
        if m.result.kind == "void":
            out.append(f"      qt6rb::call_method(qt6rb_self, rbname, {argse}, &ok);")
            out.append("      if (ok) return;")
        else:
            out.append(f"      VALUE r = qt6rb::call_method(qt6rb_self, rbname, {argse}, &ok);")
            out.append(f"      if (ok) return {m.result.to_cxx.format('r')};")
        out.append("    }")
        ret = "" if m.result.kind == "void" else "return "
        if m.pure:
            # No base implementation exists; a subclass that doesn't provide
            # one is a Ruby-level error, not undefined behaviour
            out.append(f'    rb_raise(rb_eNotImpError, "{klass.cxx}#{rname} is abstract '
                       f'and must be overridden");')
            # rb_raise does not return; this only satisfies the compiler, and
            # braced init spells "zero of this type" for pointers alike
            if m.result.kind != "void":
                out.append("    return {};")
        else:
            out.append(f"    {ret}{klass.name}::{m.name}({args});")
        out.append("  }")
    out.append("};")
    out.append("")


def emit_protected_callers(out, klass):
    """Ruby methods exposing protected virtual base implementations, so
    `super(event)` works inside Ruby overrides."""
    existing = {snake(m.name) for m in klass.methods}
    groups = {}
    for m in klass.virtuals:
        # Pure virtuals have no base implementation for super() to reach
        if m.access != AccessSpecifier.PROTECTED or m.pure:
            continue
        rname = snake(m.name)
        if rname in existing:
            continue
        # Base forwarders take the full parameter list (no default args)
        m = copy.copy(m)
        m.dispatch_min = len(m.params)
        m.prot_direct = False
        groups.setdefault(rname, []).append(m)
    # Protected non-virtuals are called directly on the shim, which made them
    # public with a `using`; their default arguments still apply
    for m in klass.prot_methods:
        rname = snake(m.name)
        if rname in existing:
            continue
        m = copy.copy(m)
        m.prot_direct = True
        groups.setdefault(rname, []).append(m)
    result = []
    for rname, overloads in groups.items():
        cname = f"rb_{klass.name}_prot_{rname}"
        out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
        out.append("  (void)argv;")
        out.append(f"  Rb_{klass.name}* shim = dynamic_cast<Rb_{klass.name}*>(static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name})));")
        out.append(f'  if (!shim) rb_raise(rb_eTypeError, "{rname} is protected; only callable on Ruby-created instances");')

        def body(m, n, indent):
            target = m.name if m.prot_direct else f"qt6rb_base_{m.name}"
            call = f"shim->{target}({conv_args(m, n)})"
            if m.result.kind == "void":
                out.append(f"{indent}{call};")
                out.append(f"{indent}return Qnil;")
            else:
                out.append(f"{indent}return {m.result.to_rb.format(call)};")

        emit_dispatch(out, overloads, f"{klass.name}#{rname}", body)
        out.append("}")
        out.append("")
        result.append((rname, cname, sorted({m.name for m in overloads})))
    return result


def emit_field(out, klass, name, ftype, writable):
    """Reader (and writer) for a public data member."""
    base = snake(name)
    getter = f"rb_{klass.name}_field_{base}"
    out.append(f"static VALUE {getter}(VALUE self) {{")
    out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    out.append(f"  return {ftype.to_rb.format('o->' + name)};")
    out.append("}")
    out.append("")
    setter = None
    if writable and ftype.to_cxx:
        setter = f"rb_{klass.name}_field_{base}_set"
        out.append(f"static VALUE {setter}(VALUE self, VALUE v) {{")
        out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
        out.append(f"  o->{name} = {ftype.to_cxx.format('v')};")
        out.append("  return v;")
        out.append("}")
        out.append("")
    return getter, setter


def signal_param_key(decls):
    """Normalized parameter-type key used to pick between overloaded signals.

    Mirrors qt6rb::signal_param_key in the runtime: drop whitespace, const,
    and reference/pointer markers so a SIGNAL() string written any of the
    usual ways ('activated(const QString&)', 'activated(QString)') selects
    the same overload."""
    parts = []
    for d in decls:
        d = re.sub(r"\bconst\b", " ", d).replace("&", " ").replace("*", " ")
        parts.append("".join(d.split()))
    return ",".join(parts)


def emit_signal(out, klass, sigs):
    """Emit the `on_<signal>` registration method for one signal name.

    Overloaded signals (QCompleter::activated(QString) vs (QModelIndex))
    would make a plain `&Klass::sig` member pointer ambiguous, so each
    overload is disambiguated with a static_cast and selected at runtime by
    the parameter types in the SIGNAL() string. With no signature (or an
    unrecognized one) the first declared overload wins."""
    name = sigs[0].name
    rname = f"on_{snake(name)}"
    cname = f"rb_{klass.name}_{rname}"
    out.append(f"static VALUE {cname}(int argc, VALUE* argv, VALUE self) {{")
    out.append(f"  {klass.name}* o = static_cast<{klass.name}*>(qt6rb::unwrap(self, &cls_{klass.name}));")
    out.append("  VALUE proc = rb_block_proc();")
    out.append("  qt6rb::retain_proc(proc);")
    overloaded = len(sigs) > 1

    def connect(sig, indent):
        lam_params = ", ".join(f"{t.cxx} a{j}" for j, t in enumerate(sig.params))
        member = f"&{klass.name}::{name}"
        if overloaded:
            # The member-pointer type must use the *declared* parameter types
            # (const QString &), not the marshalling types (QString)
            ptr_params = ", ".join(sig.param_decls)
            member = f"static_cast<void ({klass.name}::*)({ptr_params})>({member})"
        out.append(f"{indent}QObject::connect(o, {member}, o, [proc]({lam_params}) {{")
        if sig.params:
            conv = ", ".join(t.to_rb.format(f"a{j}") for j, t in enumerate(sig.params))
            out.append(f"{indent}  VALUE args[] = {{ {conv} }};")
            out.append(f"{indent}  qt6rb::call_proc(proc, {len(sig.params)}, args);")
        else:
            out.append(f"{indent}  qt6rb::call_proc(proc, 0, nullptr);")
        out.append(f"{indent}}});")

    if overloaded:
        out.append("  std::string want = qt6rb::signal_param_key(argc > 0 ? argv[0] : Qnil);")
        for sig in sigs:
            key = signal_param_key(sig.param_decls)
            out.append(f'  if (want == "{key}") {{')
            connect(sig, "    ")
            out.append("    return self;")
            out.append("  }")
        out.append("  // No (or unrecognized) signature: first declared overload wins")
    else:
        out.append("  (void)argc; (void)argv;")
    connect(sigs[0], "  ")
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
    # Alias nested types to their flattened name so every cls_/rb_ symbol
    # built from it below is a valid C identifier and a valid C++ type
    for k in classes.values():
        if k.outer:
            out.append(f"using {k.name} = {k.cxx};")
    out.append("")
    for k in classes.values():
        if k.dtor_public and not k.abstract:
            deleter = f"[](void* p) {{ delete static_cast<{k.name}*>(p); }}"
        else:
            deleter = "nullptr"
        qobj = "true" if k.is_qobject else "false"
        out.append(f'static qt6rb::ClassInfo cls_{k.name} = {{ "{k.cxx}", Qnil, {deleter}, {qobj} }};')
    out.append("")

    # Upcast thunks for every direct base, so a wrapped pointer can be walked
    # (with the compiler's pointer adjustment) to any C++ ancestor -- the Ruby
    # hierarchy only mirrors the first base
    base_edges = []
    for k in classes.values():
        for b in k.bases:
            fn = f"upcast_{k.name}_{b}"
            out.append(f"static void* {fn}(void* p) {{ "
                       f"return static_cast<{b}*>(static_cast<{k.name}*>(p)); }}")
            base_edges.append(f"  qt6rb::register_base(&cls_{k.name}, &cls_{b}, {fn});")
    out.append("")

    for k in classes.values():
        if k.shim:
            emit_shim(out, k)

    def ancestry(k):
        seen, order = set(), []

        def walk(kk):
            for b in kk.bases:
                if b not in seen:
                    seen.add(b)
                    order.append(classes[b])
                    walk(classes[b])

        walk(k)
        return order

    registrations = []
    for k in classes.values():
        if (k.ctors or k.implicit_ctor or k.name in APP_CLASSES) and not k.abstract:
            cname = emit_ctor(out, k)
            registrations.append(
                f"  rb_define_alloc_func(cls_{k.name}.rb_class, rb_{k.name}_alloc);")
            registrations.append(
                f"  qt6rb::register_ctor(cls_{k.name}.rb_class, {cname});")
            registrations.append(
                f"  rb_include_module(cls_{k.name}.rb_class, qt6rb::constructable_module());")
        else:
            registrations.append(
                f"  rb_undef_alloc_func(cls_{k.name}.rb_class);")
        for static in (False, True):
            groups = {}
            for m in k.methods:
                if m.static != static:
                    continue
                groups.setdefault(snake(m.name), []).append(m)
            # A redeclared name hides inherited overloads in C++ (and would
            # fully shadow them in Ruby); merge them back like a `using`
            for rname, overloads in groups.items():
                sigs = {(m.name, tuple(m.param_decls)) for m in overloads}
                for anc in ancestry(k):
                    for m in anc.methods:
                        if m.static != static or snake(m.name) != rname:
                            continue
                        key = (m.name, tuple(m.param_decls))
                        if key not in sigs:
                            sigs.add(key)
                            overloads.append(m)
            for rname, overloads in groups.items():
                cname = emit_method_group(out, k, rname, overloads, static)
                target = f"cls_{k.name}.rb_class"
                define = "rb_define_singleton_method" if static else "rb_define_method"
                registrations.append(
                    f'  {define}({target}, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')
                # qtbindings compatibility: also register the camelCase name
                for camel in sorted({m.name for m in overloads}):
                    if camel != rname:
                        registrations.append(
                            f'  {define}({target}, "{camel}", RUBY_METHOD_FUNC({cname}), -1);')
                if not static:
                    # setter sugar: set_interval(v) also as interval= and
                    # qtbindings camelCase intervalText= style
                    if rname.startswith("set_") and any(len(m.params) >= 1 for m in overloads):
                        registrations.append(
                            f'  rb_define_alias({target}, "{rname[4:]}=", "{rname}");')
                        for camel in sorted({m.name for m in overloads}):
                            if camel.startswith("set") and len(camel) > 3:
                                prop = camel[3].lower() + camel[4:]
                                if prop != rname[4:]:
                                    registrations.append(
                                        f'  rb_define_alias({target}, "{prop}=", "{rname}");')
                    # predicate sugar: is_active also as active?
                    if rname.startswith("is_") and all(not m.params for m in overloads) \
                            and all(m.result.kind == "bool" for m in overloads):
                        registrations.append(
                            f'  rb_define_alias({target}, "{rname[3:]}?", "{rname}");')
        # Public data members. Methods win a name clash (a class with both a
        # `text` field and a text() method keeps the method).
        method_names = set()
        for m in k.methods:
            method_names.add(m.name)
            method_names.add(snake(m.name))
        for fname, ftype, writable in k.fields:
            if fname in method_names or snake(fname) in method_names:
                continue
            getter, setter = emit_field(out, k, fname, ftype, writable)
            target = f"cls_{k.name}.rb_class"
            # sorted for deterministic generator output
            for rname in sorted({fname, snake(fname)}):
                registrations.append(
                    f'  rb_define_method({target}, "{rname}", RUBY_METHOD_FUNC({getter}), 0);')
                if setter:
                    registrations.append(
                        f'  rb_define_method({target}, "{rname}=", RUBY_METHOD_FUNC({setter}), 1);')
            if setter:
                registrations.append(
                    f'  rb_define_method({target}, "set_{snake(fname)}", RUBY_METHOD_FUNC({setter}), 1);')
        if k.shim:
            for rname, cname, camels in emit_protected_callers(out, k):
                registrations.append(
                    f'  rb_define_method(cls_{k.name}.rb_class, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')
                for camel in camels:
                    if camel != rname:
                        registrations.append(
                            f'  rb_define_method(cls_{k.name}.rb_class, "{camel}", RUBY_METHOD_FUNC({cname}), -1);')
        # Signals, grouped by name so overloads share one on_<signal> method
        # that selects the overload from the SIGNAL() signature
        groups = {}
        for s in k.signals:
            groups.setdefault(s.name, []).append(s)
        for sigs in groups.values():
            # Distinct C++ signatures only (a redeclaration would make the
            # static_cast ambiguous again). Truncated signatures can't be
            # named in a member-pointer cast, so they can't take part in an
            # overload set.
            seen_keys, unique = set(), []
            for s in sigs:
                if len(sigs) > 1 and s.truncated:
                    k.skipped += 1
                    continue
                key = signal_param_key(s.param_decls)
                if key not in seen_keys:
                    seen_keys.add(key)
                    unique.append(s)
            if not unique:
                continue
            rname, cname = emit_signal(out, k, unique)
            registrations.append(
                f'  rb_define_method(cls_{k.name}.rb_class, "{rname}", RUBY_METHOD_FUNC({cname}), -1);')

    out.append('extern "C" void Init_qt6() {')
    out.append("  VALUE mQt = qt6rb::module_qt();")
    out.append("  qt6rb::init_core(mQt);")
    emitted = set()

    def register(k):
        if k.name in emitted:
            return
        for b in k.bases:
            register(classes[b])
        super_expr = f"cls_{k.bases[0]}.rb_class" if k.bases else "Qnil"
        if k.outer:
            # Nested types keep their Qt nesting in Ruby too
            # (QTextEdit::ExtraSelection -> Qt::TextEdit::ExtraSelection)
            register(classes[k.outer])
            leaf = k.cxx.rsplit("::", 1)[1]
            out.append(f'  qt6rb::define_class_under(&cls_{k.name}, '
                       f'cls_{k.outer}.rb_class, "{leaf}", {super_expr});')
        else:
            rb_name = k.cxx[1:] if k.cxx.startswith("Q") else k.cxx
            out.append(f'  qt6rb::define_class(&cls_{k.name}, "{rb_name}", {super_expr});')
        emitted.add(k.name)

    for k in classes.values():
        register(k)
    out.extend(base_edges)
    out.extend(registrations)
    for k in classes.values():
        for e in k.enums:
            for c in e.get_children():
                if c.kind == CursorKind.ENUM_CONSTANT_DECL:
                    name = const_name(c.spelling)
                    if re.match(r'^[A-Z][A-Za-z0-9_]*$', name):
                        out.append(f'  rb_define_const(cls_{k.name}.rb_class, "{name}", INT2NUM({c.enum_value}));')
    # QVariant bridging for value classes (QSettings values etc.)
    variant_value_classes = ["QSize", "QSizeF", "QPoint", "QPointF", "QRect",
                             "QRectF", "QColor", "QFont", "QKeySequence",
                             "QUrl", "QDate", "QIcon", "QPixmap"]
    for name in variant_value_classes:
        if name in classes and not classes[name].is_qobject:
            out.append(
                f"  qt6rb::register_variant_handler(QMetaType::{name}, &cls_{name}, "
                f"[](void* p) {{ return QVariant::fromValue(*static_cast<{name}*>(p)); }}, "
                f"[](const QVariant& v) {{ return (void*)new {name}(v.value<{name}>()); }});")
    lowercase = {}
    for name, value in ns_constants.items():
        cname = const_name(name)
        if re.match(r'^[A-Z][A-Za-z0-9_]*$', cname):
            # Class names (Widget, Window, Dialog, ...) win over enum values
            out.append(f'  if (!rb_const_defined(mQt, rb_intern("{cname}"))) '
                       f'rb_define_const(mQt, "{cname}", INT2NUM({value}));')
        if re.match(r'^[a-z][A-Za-z0-9_]*$', name):
            lowercase[name] = value
    # Lowercase enum values (Qt::red etc.) exposed for a method_missing shim
    out.append("  VALUE lower = rb_hash_new();")
    for name, value in lowercase.items():
        out.append(f'  rb_hash_aset(lower, ID2SYM(rb_intern("{name}")), INT2NUM({value}));')
    out.append('  rb_define_const(mQt, "LOWERCASE_ENUMS", lower);')
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
              f"{len(k.signals)} signals, {len(k.virtuals)} virtual hooks, "
              f"{k.skipped} skipped"
              f"{' (QObject)' if k.is_qobject else ''}")
    if ns_constants:
        print(f"Qt namespace constants: {len(ns_constants)}")
    print(f"-> {opts.output}")


if __name__ == "__main__":
    main()
