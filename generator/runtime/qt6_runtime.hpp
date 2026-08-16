// Runtime support for the libclang-generated Qt 6 Ruby bindings.
// Hand-written: object wrapping, type marshalling, signal->proc plumbing.
#ifndef QT6_RUBY_RUNTIME_HPP
#define QT6_RUBY_RUNTIME_HPP

#include <ruby.h>
#include <QString>
#include <QByteArray>
#include <QObject>
#include <QCoreApplication>

namespace qt6rb {

// Per-class registration info
struct ClassInfo {
  const char* cxx_name;
  VALUE rb_class;
  // Deletes the C++ object with the correct static type
  void (*deleter)(void*);
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

// Wrap a QObject*, transferring ownership to Qt parentage rules:
// owned by Ruby only when it has no parent.
VALUE wrap_qobject(QObject* obj, ClassInfo* cls);

// Marshalling
QString to_qstring(VALUE v);
VALUE from_qstring(const QString& s);
QByteArray to_qbytearray(VALUE v);
VALUE from_qbytearray(const QByteArray& b);

// Keep a Ruby proc alive for the lifetime of the process (signal handlers)
void retain_proc(VALUE proc);
// Invoke a proc with argc/argv, reporting (not swallowing) exceptions
VALUE call_proc(VALUE proc, int argc, const VALUE* argv);

// Hand-written QCoreApplication wrapper (its argc&/argv ctor needs
// stable storage) plus Init registration for the core hand-written classes.
void init_core(VALUE mQt);

} // namespace qt6rb

#endif
