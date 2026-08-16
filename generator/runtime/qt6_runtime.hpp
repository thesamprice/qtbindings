// Runtime support for the libclang-generated Qt 6 Ruby bindings.
// Hand-written: object wrapping, type marshalling, signal->proc plumbing.
#ifndef QT6_RUBY_RUNTIME_HPP
#define QT6_RUBY_RUNTIME_HPP

#include <ruby.h>
#include <QString>
#include <QByteArray>
#include <QStringList>
#include <QVariant>
#include <QObject>
#include <QCoreApplication>

namespace qt6rb {

// Per-class registration info
struct ClassInfo {
  const char* cxx_name;
  VALUE rb_class;
  // Deletes the C++ object with the correct static type (nullptr: never owned)
  void (*deleter)(void*);
  // True for QObject-derived classes (parent-managed lifetime)
  bool is_qobject;
};

// One wrapped C++ object
struct Wrapper {
  void* ptr;
  bool owned;          // Ruby owns and should delete on GC
  ClassInfo* cls;
};

extern const rb_data_type_t wrapper_type;

VALUE module_qt();

// Register a class named `name` under module Qt with the given superclass
// (Qnil for rb_cObject)
VALUE define_class(ClassInfo* info, const char* name, VALUE superclass);

// Wrap a C++ object. If owned, the wrapper deletes it on GC.
VALUE wrap(void* ptr, ClassInfo* cls, bool owned);

// Fetch the C++ pointer from a wrapped object, checking it is a `cls`
// (or raise). Returns nullptr for nil.
void* unwrap(VALUE obj, ClassInfo* cls);

// Like unwrap but raises on nil (for by-value/reference parameters)
void* unwrap_ref(VALUE obj, ClassInfo* cls);

// Wrap a QObject*. Qt parentage manages QObject lifetimes, so these
// wrappers never delete on GC (top-level objects live for the app;
// proper destroyed()-tracking is future work).
VALUE wrap_qobject(QObject* obj, ClassInfo* cls);

// Marshalling
QString to_qstring(VALUE v);
VALUE from_qstring(const QString& s);
QByteArray to_qbytearray(VALUE v);
VALUE from_qbytearray(const QByteArray& b);
QStringList to_qstringlist(VALUE v);
VALUE from_qstringlist(const QStringList& list);
QVariant to_qvariant(VALUE v);
VALUE from_qvariant(const QVariant& v);

// Keep a Ruby proc alive for the lifetime of the process (signal handlers)
void retain_proc(VALUE proc);
// Invoke a proc with argc/argv, reporting (not swallowing) exceptions
VALUE call_proc(VALUE proc, int argc, const VALUE* argv);

// Hand-written application classes (their argc&/argv ctors need stable
// storage): Qt::CoreApplication always; Qt::Application when built with
// QtWidgets.
void init_core(VALUE mQt);

} // namespace qt6rb

#endif
