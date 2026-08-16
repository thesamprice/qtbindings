# libclang-based Qt binding generator

Replaces the smoke/smokegen toolchain (a bespoke C++ parser frozen at Qt 4.8)
with a two-stage pipeline built on libclang's Python API (`clang.cindex`):

```
Qt headers --(1) parse_qt.py/libclang--> qt_ir.json --(2) codegen--> C++ Ruby glue
```

1. **`parse_qt.py`** parses the real Qt headers with clang and emits a JSON
   intermediate representation: classes, public bases, constructors, methods
   (argument types, defaults, const/static/virtual), enums, and signal/slot
   classification. Signals and slots are recovered by redefining
   `QT_ANNOTATE_ACCESS_SPECIFIER` so `Q_SIGNALS:`/`Q_SLOTS:` sections tag
   their members with clang `annotate` attributes.

2. **`codegen.py`** walks the clang AST directly (the JSON IR is a debug
   aid only) and emits C++ that registers each class with Ruby:
   - constructors and snake_cased methods, overloads dispatched by arity
     plus runtime type guards
   - signals as `on_<signal> { }` block methods using compile-time
     member-pointer `QObject::connect` functors — no moc in the pipeline
   - base classes pulled in automatically so inheritance chains
     (`Qt::PushButton < Qt::AbstractButton < Qt::Widget < Qt::Object`)
     stay intact
   - enums as class constants, plus `--namespace-enums` for the ~1200
     `Qt::` namespace values (`Qt::AlignCenter`, ...); class names win
     collisions (`Qt::Widget` stays a class, not `Qt::WindowType::Widget`)
   - marshalling: bool/ints/floats/enums, `QFlags<>` as Integer,
     QString/QByteArray ⇄ String, QStringList ⇄ Array, QVariant ⇄ native
     Ruby values (recursing through Array/Hash), pointers to generated
     QObject classes, and by-value/const& use of generated value classes
     (QSize, QPoint, QRect)

   The hand-written runtime (`runtime/`) provides TypedData wrapping,
   the marshalling helpers, GC-safe proc retention with `rb_protect`
   around signal callbacks, and `Qt::CoreApplication`/`Qt::Application`
   (their `argc&`/`argv` constructors need stable storage).

## Building and trying it

```sh
generator-venv/bin/python generator/codegen.py \
    --qt-prefix /opt/homebrew/opt/qt \
    --modules QtCore QtGui QtWidgets --namespace-enums \
    --classes QObject QTimer QWidget QLabel QPushButton QCheckBox \
              QComboBox QLineEdit QTextEdit QMainWindow QLayout QBoxLayout \
              QVBoxLayout QHBoxLayout QGridLayout QSize QPoint QRect \
    -o ext/qt6/qt6_generated.cpp
cd ext/qt6 && ruby extconf.rb && make
ruby examples/qt6_widgets_demo.rb   # headless (offscreen platform)
```

## Known limitations (next steps)

- Ruby subclasses cannot override C++ virtuals yet (no override hooks)
- QObject wrappers never delete on GC (Qt parentage or app teardown owns
  them; proper `destroyed()` tracking is future work)
- Multiple-inheritance pointer casts assume the QObject branch is the
  first base (true across Qt)
- Overloaded signals are skipped (member pointer would be ambiguous)

## Setup

```sh
python3 -m venv generator-venv
generator-venv/bin/pip install libclang
```

## Usage

```sh
generator-venv/bin/python generator/parse_qt.py \
    --qt-prefix /opt/homebrew/opt/qt \
    --modules QtCore QtGui QtWidgets \
    -o qt_ir.json
```

Requires Qt ≥ 6 headers (`brew install qt` on macOS; needs C++17).
