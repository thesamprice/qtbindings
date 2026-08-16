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

2. **codegen** (next stage, not yet implemented) consumes the IR and emits
   C++ files that register each class with Ruby: method dispatch with
   overload resolution, constructor/ownership tracking, virtual-method
   override hooks so Ruby subclasses work, and marshalling for core types
   (QString ⇄ String, containers ⇄ Array/Hash, QVariant ⇄ Object).

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
