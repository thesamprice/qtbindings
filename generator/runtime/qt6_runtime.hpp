// Runtime support for the libclang-generated Qt 6 Ruby bindings.
// Hand-written: object wrapping, type marshalling, signal->proc plumbing.
#ifndef QT6_RUBY_RUNTIME_HPP
#define QT6_RUBY_RUNTIME_HPP

#include <ruby.h>
#include <string>
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

// Common base of every generated Rb_* shim class. It carries the Ruby object
// that constructed (and owns the behaviour of) this C++ instance, which serves
// two purposes: the shim's virtual overrides dispatch back into it, and
// wrap_qobject() uses it to hand back the *original* Ruby object rather than
// minting a fresh wrapper. Without that, a Ruby subclass handed to Qt and
// returned by some accessor (QApplication::activeModalWidget, sender(),
// parentWidget(), QTabWidget::widget(), ...) came back as a plain generated
// class, losing the subclass identity and its instance variables.
struct RubyPeer {
  VALUE qt6rb_self = Qnil;
  void qt6rb_set_self(VALUE v) { qt6rb_self = v; rb_gc_register_address(&qt6rb_self); }
  virtual ~RubyPeer();
};

VALUE module_qt();

// Register a class named `name` under module Qt with the given superclass
// (Qnil for rb_cObject)
VALUE define_class(ClassInfo* info, const char* name, VALUE superclass);
// Same, but nested under an existing class rather than under module Qt
// (QTextEdit::ExtraSelection -> Qt::TextEdit::ExtraSelection)
VALUE define_class_under(ClassInfo* info, VALUE outer, const char* name,
                         VALUE superclass);

// Wrap a C++ object. If owned, the wrapper deletes it on GC.
VALUE wrap(void* ptr, ClassInfo* cls, bool owned);

// Allocate an empty (unconstructed) wrapper for klass; #initialize attaches
// the C++ object. Enables idiomatic Ruby subclassing with super().
VALUE alloc_wrapper(VALUE klass, ClassInfo* cls);

// Attach a freshly constructed C++ object to an allocated wrapper
void attach(VALUE self, void* ptr, bool owned);

// Fetch the C++ pointer from a wrapped object, checking it is a `cls`
// (or raise). Returns nullptr for nil.
void* unwrap(VALUE obj, ClassInfo* cls);

// Like unwrap but raises on nil (for by-value/reference parameters)
void* unwrap_ref(VALUE obj, ClassInfo* cls);

// unwrap for pointer parameters: Qt APIs taking object pointers generally
// take ownership, so Ruby relinquishes its ownership of the passed object
void* unwrap_release(VALUE obj, ClassInfo* cls);

// C++ base registration. The Ruby class hierarchy can only mirror one base,
// so classes with several (QWidget is a QObject *and* a QPaintDevice) record
// the others here together with a thunk that performs the pointer adjustment
// the compiler would apply for the upcast.
typedef void* (*UpcastFn)(void*);
void register_base(ClassInfo* derived, ClassInfo* base, UpcastFn fn);

// True when obj is usable as a `cls`, through either the Ruby class
// hierarchy or a registered secondary C++ base
bool is_kind_of(VALUE obj, ClassInfo* cls);

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

// QList of a generated value class <-> Ruby Array. Elements are copied in
// both directions, matching Qt's by-value container semantics: mutating an
// element after handing the list to Qt has no effect, exactly as in C++.
template <typename T>
QList<T> to_objlist(VALUE v, ClassInfo* cls) {
  QList<T> list;
  if (NIL_P(v)) return list;
  Check_Type(v, T_ARRAY);
  long n = RARRAY_LEN(v);
  list.reserve(n);
  for (long i = 0; i < n; ++i)
    list.append(*static_cast<T*>(unwrap_ref(rb_ary_entry(v, i), cls)));
  return list;
}

template <typename T>
VALUE from_objlist(const QList<T>& list, ClassInfo* cls) {
  VALUE ary = rb_ary_new_capa(list.size());
  for (const T& item : list)
    rb_ary_push(ary, wrap(new T(item), cls, true));
  return ary;
}

// QList of pointers to a generated class (QWidget::actions,
// QMenu::addActions). Ownership follows the same rule as a bare pointer:
// whoever the object is parented to keeps it alive.
template <typename T>
QList<T*> to_objptrlist(VALUE v, ClassInfo* cls) {
  QList<T*> list;
  if (NIL_P(v)) return list;
  Check_Type(v, T_ARRAY);
  long n = RARRAY_LEN(v);
  list.reserve(n);
  for (long i = 0; i < n; ++i)
    list.append(static_cast<T*>(unwrap_release(rb_ary_entry(v, i), cls)));
  return list;
}

template <typename T>
VALUE from_qobjptrlist(const QList<T*>& list, ClassInfo* cls) {
  VALUE ary = rb_ary_new_capa(list.size());
  for (T* item : list)
    rb_ary_push(ary, wrap_qobject(static_cast<QObject*>(item), cls));
  return ary;
}

template <typename T>
VALUE from_objptrlist(const QList<T*>& list, ClassInfo* cls) {
  VALUE ary = rb_ary_new_capa(list.size());
  for (T* item : list)
    rb_ary_push(ary, wrap(static_cast<void*>(item), cls, false));
  return ary;
}

// Out-parameter helper for Qt's `bool *ok` idiom (QInputDialog::getText and
// friends). Constructed as a temporary in the argument list; it converts to
// bool* for the call and, when destroyed at the end of the enclosing full
// expression (after the call has returned), writes the flag back into the
// Ruby object by calling `value=` on it. nil discards the result.
class BoolOut {
public:
  explicit BoolOut(VALUE v) : rb_(v), value_(false) {}
  ~BoolOut();
  operator bool*() { return &value_; }
  BoolOut(const BoolOut&) = delete;
  BoolOut& operator=(const BoolOut&) = delete;
private:
  VALUE rb_;
  bool value_;
};

// Value-class <-> QVariant bridging (QSize in QSettings values etc.).
// Registered by generated code for each known value class.
typedef QVariant (*ToVariantFn)(void*);
typedef void* (*FromVariantFn)(const QVariant&);
void register_variant_handler(int meta_id, ClassInfo* cls,
                              ToVariantFn to, FromVariantFn from);

// True when the Ruby instance's class overrides `name` below the generated
// class cls (i.e. a user-defined virtual override). Cached per class.
bool has_override(VALUE self, ClassInfo* cls, const char* name);

// False once the Ruby VM has been finalized (Qt atexit handlers may still
// deliver events and destroy objects after that point)
bool ruby_alive();

// Returns whichever of the two spellings (snake_case, camelCase) the user
// overrode, or nullptr. qtbindings-era code overrides camelCase names.
const char* pick_override(VALUE self, ClassInfo* cls,
                          const char* snake_name, const char* camel_name);

// Constructor registry: initialize is resolved against the *receiver's*
// nearest generated ancestor class, so reopened classes and subclasses
// construct the correct C++ type when calling super.
typedef VALUE (*CtorFn)(int argc, VALUE* argv, VALUE self);
void register_ctor(VALUE rb_class, CtorFn fn);
VALUE generic_initialize(int argc, VALUE* argv, VALUE self);

// Module providing #initialize (generic_initialize). Included beneath every
// constructible class so user reopens/subclasses can redefine initialize and
// still reach construction via super.
VALUE constructable_module();

// Stable argc/argv storage for Q*Application constructors
void app_args(VALUE rb_args, int** argc_out, char*** argv_out);

// Call a Ruby method under rb_protect; reports exceptions to stderr.
// Sets *ok=false (and returns Qnil) if the call raised.
VALUE call_method(VALUE self, const char* name, int argc, const VALUE* argv, bool* ok);

// Normalized parameter-type key of a SIGNAL() signature, used by generated
// on_<signal> methods to pick between overloaded signals ("" if absent)
std::string signal_param_key(VALUE signature);

// Keep a Ruby proc alive for the lifetime of the process (signal handlers)
void retain_proc(VALUE proc);
// Invoke a proc with argc/argv, reporting (not swallowing) exceptions
VALUE call_proc(VALUE proc, int argc, const VALUE* argv);

// Registers Qt module helpers (_dispose etc.)
void init_core(VALUE mQt);

} // namespace qt6rb

#endif
