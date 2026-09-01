#!/bin/bash
# Canonical regeneration of the Qt 6 bindings glue. Edit CLASSES to add
# classes, then run this script and `cd ext/qt6 && make`.
#
# The output is Qt-version-specific -- it binds exactly the API the local Qt
# headers declare -- so it is written to ext/qt6/generated/qt6_generated_<qt
# version>.cpp and extconf.rb picks the file matching the Qt it builds against.
# Run this once per Qt version you want to ship bindings for.
#
# Qt is located with pkg-config; set QT_PREFIX to override (a prefix with
# include/QtCore/QtCore under it, e.g. Homebrew's /opt/homebrew/opt/qt).
set -e
cd "$(dirname "$0")/.."

# Python with the clang bindings. Honour $PYTHON, else try the venv and a
# plain python3, each with generator-libs/ (a `pip install --target`) on the
# path as a fallback for systems without the venv module.
# Sets PYTHON (and PYTHONPATH when the fallback dir is used). Assigns rather
# than echoes: a command substitution would run it in a subshell and lose the
# exported PYTHONPATH.
select_python() {
  local candidate
  for candidate in ${PYTHON:+"$PYTHON"} generator-venv/bin/python python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import clang.cindex' 2>/dev/null; then
      PYTHON="$candidate"
      return 0
    fi
    if [ -d generator-libs ] &&
       PYTHONPATH="$PWD/generator-libs" "$candidate" -c 'import clang.cindex' 2>/dev/null; then
      PYTHON="$candidate"
      export PYTHONPATH="$PWD/generator-libs${PYTHONPATH:+:$PYTHONPATH}"
      return 0
    fi
  done
  return 1
}

if ! select_python; then
  echo "error: no python with the clang bindings (clang.cindex)." >&2
  echo "Install them with either:" >&2
  echo "  python3 -m venv generator-venv && generator-venv/bin/pip install libclang" >&2
  echo "  pip install --target generator-libs libclang   # no venv module" >&2
  exit 1
fi

# libclang needs its own builtin headers (stddef.h and friends). The pip
# wheel ships the library but not those, so borrow them from any clang
# installed, newest first.
if [ -z "$CPLUS_INCLUDE_PATH" ]; then
  for d in $(ls -d /usr/lib/llvm-*/lib/clang/*/include \
                   /usr/lib/clang/*/include \
                   /usr/local/lib/clang/*/include 2>/dev/null | sort -V -r); do
    if [ -e "$d/stddef.h" ]; then export CPLUS_INCLUDE_PATH="$d"; break; fi
  done
fi

QT_VERSION=$(pkg-config --modversion Qt6Core 2>/dev/null || true)
if [ -z "$QT_PREFIX" ]; then
  # Debian/Ubuntu split Qt across /usr/include/<triplet>/qt6 and
  # /usr/lib/<triplet>, which is not the include/ + lib/ prefix the parser
  # expects, so build a symlink prefix when the real one does not fit.
  inc=$(pkg-config --variable=includedir Qt6Core 2>/dev/null || true)
  lib=$(pkg-config --variable=libdir Qt6Core 2>/dev/null || true)
  if [ -z "$inc" ]; then
    echo "error: Qt 6 not found via pkg-config; set QT_PREFIX" >&2
    exit 1
  fi
  if [ -e "${inc%/include}/include/QtCore/QtCore" ]; then
    QT_PREFIX="${inc%/include}"
  else
    QT_PREFIX="$(mktemp -d)/qt"
    mkdir -p "$QT_PREFIX"
    ln -sfn "$inc" "$QT_PREFIX/include"
    ln -sfn "$lib" "$QT_PREFIX/lib"
  fi
fi
[ -n "$QT_VERSION" ] || QT_VERSION=$(basename "$(ls -d "$QT_PREFIX"/include/QtCore/*/ 2>/dev/null | head -1)")
if [ -z "$QT_VERSION" ]; then
  echo "error: could not determine the Qt version; set QT_VERSION" >&2
  exit 1
fi
OUT="ext/qt6/generated/qt6_generated_${QT_VERSION//./_}.cpp"
mkdir -p "$(dirname "$OUT")"
echo "Generating bindings for Qt $QT_VERSION from $QT_PREFIX"

CLASSES=(
  QObject QTimer QCoreApplication QGuiApplication QApplication QScreen
  QWidget QLabel QPushButton QCheckBox QComboBox QLineEdit QTextEdit QPlainTextEdit QMainWindow
  QLayout QBoxLayout QVBoxLayout QHBoxLayout QGridLayout QFormLayout QStackedLayout QSpacerItem
  QSize QPoint QRect QPointF QSizeF QRectF QDate QUrl QChar
  QEvent QTimerEvent QChildEvent QCloseEvent QShowEvent QHideEvent QMoveEvent QResizeEvent
  QPaintEvent QKeyEvent QFocusEvent QEnterEvent QMouseEvent QWheelEvent
  QColor QPen QBrush QPalette QFont QFontMetrics QCursor QIcon QPixmap QImage QPainter
  QLinearGradient QRadialGradient QPolygon
  QMessageBox QDialog QFileDialog QInputDialog QProgressBar QSlider QSpinBox QDoubleSpinBox
  QRadioButton QButtonGroup QDialogButtonBox QCalendarWidget
  QTableWidget QTableWidgetItem QTreeWidget QTreeWidgetItem QTabWidget QTabBar QListWidget QListWidgetItem
  QTextCursor QTextDocument QTextDocumentFragment QTextCharFormat QSyntaxHighlighter QTextOption
  QTextEdit::ExtraSelection QTextBlock QTextLayout QTextLayout::FormatRange
  QScrollBar QAbstractItemView QHeaderView QGroupBox QSplitter QScrollArea QStatusBar
  QMenuBar QMenu QAction QActionGroup QToolBar QDesktopServices
  QValidator QIntValidator QDoubleValidator QKeySequence QSizePolicy QSettings QModelIndex
  QStringListModel QAbstractItemModel QFileSystemModel QListView QTreeView QCompleter QEventLoop
  QMimeData QMovie QStyledItemDelegate QStyleOptionViewItem QStyleOptionButton QFrame
  QShortcut QStyle QPaintEngine
)

exec $PYTHON generator/codegen.py \
  --qt-prefix "$QT_PREFIX" \
  --modules QtCore QtGui QtWidgets \
  --namespace-enums \
  --classes "${CLASSES[@]}" \
  -o "$OUT" "$@"
