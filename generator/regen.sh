#!/bin/bash
# Canonical regeneration of the Qt 6 bindings glue. Edit CLASSES to add
# classes, then run this script and `cd ext/qt6 && make`.
set -e
cd "$(dirname "$0")/.."

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

exec generator-venv/bin/python generator/codegen.py \
  --qt-prefix /opt/homebrew/opt/qt \
  --modules QtCore QtGui QtWidgets \
  --namespace-enums \
  --classes "${CLASSES[@]}" \
  -o ext/qt6/qt6_generated.cpp "$@"
