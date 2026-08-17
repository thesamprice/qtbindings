#include "qt6_runtime.hpp"
#include <QEvent>
#include <vector>
#include <string>
#include <cstring>

#if defined(QT_WIDGETS_LIB) && __has_include(<QApplication>)
#include <QApplication>
#define QT6RB_HAVE_WIDGETS 1
#endif

namespace qt6rb {

static VALUE s_module_qt = Qnil;
static VALUE s_proc_registry = Qnil;

// Qt's atexit handlers flush posted events (deferred deletes) after the
// Ruby VM is finalized; shims and signal lambdas must not call into Ruby
// once it is gone.
static bool s_ruby_alive = true;
static void mark_ruby_dead(VALUE) {
  // Flush pending deferred deletes while Ruby and the platform plugin are
  // both still alive; the atexit flush is too late (QBackingStore teardown
  // deadlocks after the platform plugin is finalized)
  if (QCoreApplication::instance()) {
    QCoreApplication::sendPostedEvents(nullptr, QEvent::DeferredDelete);
  }
  s_ruby_alive = false;
}

bool ruby_alive() { return s_ruby_alive; }

static void wrapper_free(void* data) {
  Wrapper* w = static_cast<Wrapper*>(data);
  if (w->owned && w->ptr && w->cls && w->cls->deleter) {
    w->cls->deleter(w->ptr);
  }
  xfree(w);
}

static size_t wrapper_memsize(const void*) { return sizeof(Wrapper); }

const rb_data_type_t wrapper_type = {
  "Qt::Wrapper",
  { nullptr, wrapper_free, wrapper_memsize, },
  nullptr, nullptr,
  RUBY_TYPED_FREE_IMMEDIATELY,
};

VALUE module_qt() {
  if (NIL_P(s_module_qt)) {
    s_module_qt = rb_define_module("Qt");
    rb_gc_register_address(&s_module_qt);
  }
  return s_module_qt;
}

#include <string>
#include <map>
static std::map<std::string, ClassInfo*>* s_qobject_classes = nullptr;

VALUE define_class(ClassInfo* info, const char* name, VALUE superclass) {
  VALUE super = NIL_P(superclass) ? rb_cObject : superclass;
  info->rb_class = rb_define_class_under(module_qt(), name, super);
  rb_gc_register_address(&info->rb_class);
  if (info->is_qobject) {
    if (!s_qobject_classes) s_qobject_classes = new std::map<std::string, ClassInfo*>();
    (*s_qobject_classes)[info->cxx_name] = info;
  }
  return info->rb_class;
}

VALUE wrap(void* ptr, ClassInfo* cls, bool owned) {
  if (!ptr) return Qnil;
  Wrapper* w = static_cast<Wrapper*>(xmalloc(sizeof(Wrapper)));
  w->ptr = ptr;
  w->owned = owned;
  w->cls = cls;
  return TypedData_Wrap_Struct(cls->rb_class, &wrapper_type, w);
}

// derived -> [(direct base, upcast thunk)] for every C++ base, so a pointer
// can be walked (and adjusted) to any ancestor, including the ones the
// single-inheritance Ruby hierarchy cannot express
struct BaseEdge { ClassInfo* base; UpcastFn fn; };
static std::map<ClassInfo*, std::vector<BaseEdge> >* s_extra_bases = nullptr;

void register_base(ClassInfo* derived, ClassInfo* base, UpcastFn fn) {
  if (!s_extra_bases) s_extra_bases = new std::map<ClassInfo*, std::vector<BaseEdge> >();
  BaseEdge e = { base, fn };
  (*s_extra_bases)[derived].push_back(e);
}

// Walks the recorded secondary-base edges from `from` to `to`, applying each
// thunk, and returns the adjusted pointer (or nullptr when unreachable).
static void* upcast(void* ptr, ClassInfo* from, ClassInfo* to) {
  if (from == to) return ptr;
  if (!s_extra_bases) return nullptr;
  auto it = s_extra_bases->find(from);
  if (it == s_extra_bases->end()) return nullptr;
  for (const BaseEdge& e : it->second) {
    void* adjusted = e.fn(ptr);
    if (void* result = upcast(adjusted, e.base, to)) return result;
  }
  return nullptr;
}

bool is_kind_of(VALUE obj, ClassInfo* cls) {
  if (NIL_P(obj) || !cls) return true;
  if (rb_obj_is_kind_of(obj, cls->rb_class)) return true;
  if (!rb_typeddata_is_kind_of(obj, &wrapper_type)) return false;
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  return w->ptr && upcast(w->ptr, w->cls, cls) != nullptr;
}

void* unwrap(VALUE obj, ClassInfo* cls) {
  if (NIL_P(obj)) return nullptr;
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  void* adjusted = w->ptr;
  if (cls && !rb_obj_is_kind_of(obj, cls->rb_class)) {
    // Not in the Ruby hierarchy: it may still be a secondary C++ base
    // (Qt::Painter.new(widget) wants the QWidget's QPaintDevice)
    adjusted = w->ptr ? upcast(w->ptr, w->cls, cls) : nullptr;
    if (!adjusted) {
      rb_raise(rb_eTypeError, "expected %s but got %s",
               cls->cxx_name, rb_obj_classname(obj));
    }
  }
  if (!w->ptr) {
    rb_raise(rb_eRuntimeError,
             "%s used before construction (did initialize call super?)",
             rb_obj_classname(obj));
  }
  return adjusted;
}

VALUE alloc_wrapper(VALUE klass, ClassInfo* cls) {
  Wrapper* w = static_cast<Wrapper*>(xmalloc(sizeof(Wrapper)));
  w->ptr = nullptr;
  w->owned = false;
  w->cls = cls;
  return TypedData_Wrap_Struct(klass, &wrapper_type, w);
}

void attach(VALUE self, void* ptr, bool owned) {
  Wrapper* w;
  TypedData_Get_Struct(self, Wrapper, &wrapper_type, w);
  if (w->ptr) {
    rb_raise(rb_eRuntimeError, "%s already constructed (super called twice?)",
             rb_obj_classname(self));
  }
  w->ptr = ptr;
  w->owned = owned;
}

bool has_override(VALUE self, ClassInfo* cls, const char* name) {
  if (NIL_P(self)) return false;
  // rb_class_of sees per-object singleton classes (define_singleton_method)
  VALUE klass = rb_class_of(self);
  if (klass == cls->rb_class) return false;
  static ID id_cache = 0;
  if (!id_cache) id_cache = rb_intern("__qt6rb_overrides__");
  ID mid = rb_intern(name);
  VALUE key = ID2SYM(mid);
  VALUE cache = rb_ivar_get(klass, id_cache);
  if (NIL_P(cache)) {
    cache = rb_hash_new();
    rb_ivar_set(klass, id_cache, cache);
  }
  VALUE cached = rb_hash_aref(cache, key);
  if (!NIL_P(cached)) return RTEST(cached);
  bool result = false;
  if (rb_respond_to(self, mid)) {
    VALUE meth = rb_obj_method(self, key);
    VALUE owner = rb_funcall(meth, rb_intern("owner"), 0);
    VALUE ancestors = rb_mod_ancestors(klass);
    long io = -1, ic = -1;
    for (long i = 0; i < RARRAY_LEN(ancestors); i++) {
      VALUE a = rb_ary_entry(ancestors, i);
      if (io < 0 && a == owner) io = i;
      if (ic < 0 && a == cls->rb_class) ic = i;
    }
    result = (io >= 0 && ic >= 0 && io < ic);
  }
  rb_hash_aset(cache, key, result ? Qtrue : Qfalse);
  return result;
}

struct MethodCall { VALUE self; ID mid; int argc; const VALUE* argv; };

static VALUE call_method_body(VALUE arg) {
  MethodCall* mc = reinterpret_cast<MethodCall*>(arg);
  return rb_funcallv(mc->self, mc->mid, mc->argc, mc->argv);
}

VALUE call_method(VALUE self, const char* name, int argc, const VALUE* argv, bool* ok) {
  if (!s_ruby_alive || rb_during_gc()) {
    if (ok) *ok = false;
    return Qnil;
  }
  MethodCall mc = { self, rb_intern(name), argc, argv };
  int state = 0;
  VALUE result = rb_protect(call_method_body, reinterpret_cast<VALUE>(&mc), &state);
  if (state) {
    VALUE err = rb_errinfo();
    rb_set_errinfo(Qnil);
    VALUE msg = rb_funcall(err, rb_intern("full_message"), 0);
    fprintf(stderr, "Qt virtual override raised:\n%s\n", StringValueCStr(msg));
    if (ok) *ok = false;
    return Qnil;
  }
  if (ok) *ok = true;
  return result;
}

void* unwrap_release(VALUE obj, ClassInfo* cls) {
  if (NIL_P(obj)) return nullptr;
  void* ptr = unwrap(obj, cls);
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  w->owned = false;
  return ptr;
}

void* unwrap_ref(VALUE obj, ClassInfo* cls) {
  if (NIL_P(obj)) {
    rb_raise(rb_eTypeError, "expected %s but got nil", cls ? cls->cxx_name : "Qt object");
  }
  return unwrap(obj, cls);
}

RubyPeer::~RubyPeer() {
  if (ruby_alive() && !NIL_P(qt6rb_self)) rb_gc_unregister_address(&qt6rb_self);
}

VALUE wrap_qobject(QObject* obj, ClassInfo* cls) {
  if (!obj) return Qnil;
  // If this object was constructed from Ruby it already has a Ruby peer;
  // return that object so identity (and any Ruby subclass, its methods and
  // its instance variables) survives a round trip through Qt. The cross-cast
  // is safe: QObject is polymorphic and every generated shim derives from
  // both the Qt class and RubyPeer.
  if (RubyPeer* peer = dynamic_cast<RubyPeer*>(obj)) {
    if (!NIL_P(peer->qt6rb_self)) return peer->qt6rb_self;
  }
  // Downcast to the most-derived generated class via the meta-object, so
  // e.g. activeModalWidget returns a Qt::MessageBox, not a Qt::Widget
  if (s_qobject_classes) {
    for (const QMetaObject* mo = obj->metaObject(); mo; mo = mo->superClass()) {
      auto it = s_qobject_classes->find(mo->className());
      if (it != s_qobject_classes->end()) {
        cls = it->second;
        break;
      }
    }
  }
  // Never owned: Qt's parent/child system (or the app teardown) deletes
  // QObjects; deleting from GC would double-free reparented objects.
  return wrap(obj, cls, false);
}

QString to_qstring(VALUE v) {
  StringValue(v);
  return QString::fromUtf8(RSTRING_PTR(v), (int)RSTRING_LEN(v));
}

VALUE from_qstring(const QString& s) {
  QByteArray utf8 = s.toUtf8();
  return rb_utf8_str_new(utf8.constData(), utf8.size());
}

QByteArray to_qbytearray(VALUE v) {
  StringValue(v);
  return QByteArray(RSTRING_PTR(v), (int)RSTRING_LEN(v));
}

VALUE from_qbytearray(const QByteArray& b) {
  return rb_str_new(b.constData(), b.size());
}

QStringList to_qstringlist(VALUE v) {
  Check_Type(v, T_ARRAY);
  QStringList list;
  for (long i = 0; i < RARRAY_LEN(v); i++) {
    list << to_qstring(rb_ary_entry(v, i));
  }
  return list;
}

VALUE from_qstringlist(const QStringList& list) {
  VALUE ary = rb_ary_new_capa(list.size());
  for (const QString& s : list) rb_ary_push(ary, from_qstring(s));
  return ary;
}

#include <map>
struct VariantHandler { ClassInfo* cls; ToVariantFn to; FromVariantFn from; };
static std::map<int, VariantHandler>* s_variant_by_meta = nullptr;
static std::map<ClassInfo*, VariantHandler>* s_variant_by_cls = nullptr;

void register_variant_handler(int meta_id, ClassInfo* cls,
                              ToVariantFn to, FromVariantFn from) {
  if (!s_variant_by_meta) {
    s_variant_by_meta = new std::map<int, VariantHandler>();
    s_variant_by_cls = new std::map<ClassInfo*, VariantHandler>();
  }
  VariantHandler h = { cls, to, from };
  (*s_variant_by_meta)[meta_id] = h;
  (*s_variant_by_cls)[cls] = h;
}

static int hash_to_qvariantmap_i(VALUE key, VALUE val, VALUE arg) {
  QVariantMap* map = reinterpret_cast<QVariantMap*>(arg);
  map->insert(to_qstring(rb_obj_as_string(key)), to_qvariant(val));
  return ST_CONTINUE;
}

QVariant to_qvariant(VALUE v) {
  switch (TYPE(v)) {
    case T_NIL:    return QVariant();
    case T_TRUE:   return QVariant(true);
    case T_FALSE:  return QVariant(false);
    case T_FIXNUM:
    case T_BIGNUM: return QVariant(static_cast<qlonglong>(NUM2LL(v)));
    case T_FLOAT:  return QVariant(NUM2DBL(v));
    case T_STRING: return QVariant(to_qstring(v));
    case T_SYMBOL: return QVariant(to_qstring(rb_sym2str(v)));
    case T_ARRAY: {
      QVariantList list;
      for (long i = 0; i < RARRAY_LEN(v); i++) {
        list << to_qvariant(rb_ary_entry(v, i));
      }
      return QVariant(list);
    }
    case T_HASH: {
      QVariantMap map;
      rb_hash_foreach(v, hash_to_qvariantmap_i, reinterpret_cast<VALUE>(&map));
      return QVariant(map);
    }
    default:
      if (rb_typeddata_is_kind_of(v, &wrapper_type)) {
        Wrapper* w;
        TypedData_Get_Struct(v, Wrapper, &wrapper_type, w);
        if (w->cls && s_variant_by_cls) {
          auto it = s_variant_by_cls->find(w->cls);
          if (it != s_variant_by_cls->end()) return it->second.to(w->ptr);
        }
        if (w->cls && w->cls->is_qobject) {
          return QVariant::fromValue(static_cast<QObject*>(w->ptr));
        }
      }
      rb_raise(rb_eTypeError, "cannot convert %s to QVariant", rb_obj_classname(v));
  }
}

VALUE from_qvariant(const QVariant& v) {
  if (!v.isValid() || v.isNull()) return Qnil;
  switch (v.metaType().id()) {
    case QMetaType::Bool:      return v.toBool() ? Qtrue : Qfalse;
    case QMetaType::Int:       return INT2NUM(v.toInt());
    case QMetaType::UInt:      return UINT2NUM(v.toUInt());
    case QMetaType::Long:
    case QMetaType::LongLong:  return LL2NUM(v.toLongLong());
    case QMetaType::ULong:
    case QMetaType::ULongLong: return ULL2NUM(v.toULongLong());
    case QMetaType::Float:
    case QMetaType::Double:    return DBL2NUM(v.toDouble());
    case QMetaType::QString:   return from_qstring(v.toString());
    case QMetaType::QByteArray: return from_qbytearray(v.toByteArray());
    case QMetaType::QStringList: return from_qstringlist(v.toStringList());
    case QMetaType::QVariantList: {
      VALUE ary = rb_ary_new();
      for (const QVariant& e : v.toList()) rb_ary_push(ary, from_qvariant(e));
      return ary;
    }
    case QMetaType::QVariantMap: {
      VALUE hash = rb_hash_new();
      QVariantMap map = v.toMap();
      for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
        rb_hash_aset(hash, from_qstring(it.key()), from_qvariant(it.value()));
      }
      return hash;
    }
    default:
      if (s_variant_by_meta) {
        auto it = s_variant_by_meta->find(v.metaType().id());
        if (it != s_variant_by_meta->end()) {
          return wrap(it->second.from(v), it->second.cls, true);
        }
      }
      if (v.canConvert<QString>()) return from_qstring(v.toString());
      return Qnil;
  }
}

// Writes the `bool *ok` result back into the Ruby out-param object. Runs at
// the end of the full expression containing the wrapped call, so the flag is
// final by then. Anything that does not accept `value=` (nil in particular)
// simply discards it.
BoolOut::~BoolOut() {
  if (!ruby_alive() || NIL_P(rb_)) return;
  if (rb_respond_to(rb_, rb_intern("value="))) {
    bool ok = true;
    VALUE arg = value_ ? Qtrue : Qfalse;
    call_method(rb_, "value=", 1, &arg, &ok);
  }
}

void retain_proc(VALUE proc) {
  if (NIL_P(s_proc_registry)) {
    s_proc_registry = rb_ary_new();
    rb_gc_register_address(&s_proc_registry);
  }
  rb_ary_push(s_proc_registry, proc);
}

std::string signal_param_key(VALUE signature) {
  // "activated(const QString&)" -> "QString"; used to pick between
  // overloaded signals. Whitespace, const and &/* are dropped so any of the
  // usual SIGNAL() spellings select the same overload. Returns "" for nil,
  // a non-String, or a signature with no parameter list.
  if (NIL_P(signature) || !RB_TYPE_P(signature, T_STRING)) return std::string();
  std::string s(RSTRING_PTR(signature), RSTRING_LEN(signature));
  std::string::size_type open = s.find('(');
  std::string::size_type close = s.rfind(')');
  if (open == std::string::npos || close == std::string::npos || close < open)
    return std::string();
  std::string params = s.substr(open + 1, close - open - 1);
  std::string out;
  int depth = 0;
  for (std::string::size_type i = 0; i < params.size(); ++i) {
    char c = params[i];
    if (c == '<') depth++;
    if (c == '>') depth--;
    if (c == '&' || c == '*' || isspace(static_cast<unsigned char>(c))) continue;
    if (c == ',' && depth == 0) { out += ','; continue; }
    // Drop the "const" keyword (only ever appears at a token boundary)
    if (c == 'c' && params.compare(i, 5, "const") == 0) {
      bool at_start = (i == 0);
      if (!at_start) {
        char prev = params[i - 1];
        at_start = isspace(static_cast<unsigned char>(prev)) || prev == ',';
      }
      if (at_start) { i += 4; continue; }
    }
    out += c;
  }
  return out;
}

struct ProcCall { VALUE proc; int argc; const VALUE* argv; };

static VALUE call_proc_body(VALUE arg) {
  ProcCall* pc = reinterpret_cast<ProcCall*>(arg);
  return rb_funcallv(pc->proc, rb_intern("call"), pc->argc, pc->argv);
}

VALUE call_proc(VALUE proc, int argc, const VALUE* argv) {
  if (!s_ruby_alive || rb_during_gc()) return Qnil;
  ProcCall pc = { proc, argc, argv };
  int state = 0;
  VALUE result = rb_protect(call_proc_body, reinterpret_cast<VALUE>(&pc), &state);
  if (state) {
    // Don't let a Ruby exception unwind through Qt's event loop; report it
    VALUE err = rb_errinfo();
    rb_set_errinfo(Qnil);
    VALUE msg = rb_funcall(err, rb_intern("full_message"), 0);
    fprintf(stderr, "Qt signal handler raised:\n%s\n", StringValueCStr(msg));
    return Qnil;
  }
  return result;
}

const char* pick_override(VALUE self, ClassInfo* cls,
                          const char* snake_name, const char* camel_name) {
  // Virtuals fire during C++ teardown of GC-collected objects; calling into
  // Ruby (or even allocating) inside the GC phase is fatal
  if (!s_ruby_alive || rb_during_gc()) return nullptr;
  if (has_override(self, cls, snake_name)) return snake_name;
  if (camel_name && strcmp(camel_name, snake_name) != 0 &&
      has_override(self, cls, camel_name)) return camel_name;
  return nullptr;
}

// ---------------------------------------------------------------------------
// Constructor resolution
// ---------------------------------------------------------------------------

#include <map>
static std::map<VALUE, CtorFn>* s_ctors = nullptr;

void register_ctor(VALUE rb_class, CtorFn fn) {
  if (!s_ctors) s_ctors = new std::map<VALUE, CtorFn>();
  (*s_ctors)[rb_class] = fn;
}

VALUE generic_initialize(int argc, VALUE* argv, VALUE self) {
  VALUE klass = rb_obj_class(self);
  while (!NIL_P(klass)) {
    if (s_ctors) {
      auto it = s_ctors->find(klass);
      if (it != s_ctors->end()) return it->second(argc, argv, self);
    }
    klass = rb_class_superclass(klass);
  }
  rb_raise(rb_eRuntimeError, "no constructor registered for %s",
           rb_obj_classname(self));
}

// ---------------------------------------------------------------------------
// Application argc/argv storage
// ---------------------------------------------------------------------------

struct AppArgs {
  int argc = 0;
  std::vector<char*> argv;
  std::vector<std::string> storage;
};
static AppArgs s_app_args;

void app_args(VALUE rb_args, int** argc_out, char*** argv_out) {
  s_app_args.storage.clear();
  s_app_args.argv.clear();
  s_app_args.storage.push_back("ruby");
  if (!NIL_P(rb_args)) {
    Check_Type(rb_args, T_ARRAY);
    for (long i = 0; i < RARRAY_LEN(rb_args); i++) {
      VALUE e = rb_ary_entry(rb_args, i);
      s_app_args.storage.push_back(StringValueCStr(e));
    }
  }
  for (auto& str : s_app_args.storage) s_app_args.argv.push_back(str.data());
  s_app_args.argc = (int)s_app_args.argv.size();
  *argc_out = &s_app_args.argc;
  *argv_out = s_app_args.argv.data();
}

// ---------------------------------------------------------------------------
// Qt module helpers
// ---------------------------------------------------------------------------

// Qt._dispose(obj): owned objects are deleted now; unowned QObjects get
// deleteLater; anything else is just detached. Wrapper is marked disposed.
static VALUE qt_dispose(VALUE mod, VALUE obj) {
  (void)mod;
  if (NIL_P(obj)) return Qnil;
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  if (w->ptr) {
    if (w->owned && w->cls && w->cls->deleter) {
      w->cls->deleter(w->ptr);
    } else if (w->cls && w->cls->is_qobject) {
      QObject* qobj = static_cast<QObject*>(w->ptr);
      // deleteLater() defers destruction to the next event loop pass, so Qt
      // can still deliver events (a QWidget keeps getting paintEvent) to a
      // shim whose wrapper no longer holds a pointer. Dispatching those into
      // Ruby means the override runs against a detached wrapper -- e.g.
      // LineGraph#paintEvent doing Qt::Painter.new(self) raises "expected
      // QPaintDevice". Drop the peer link so virtuals fall back to the C++
      // base implementations for the object's remaining lifetime.
      if (RubyPeer* peer = dynamic_cast<RubyPeer*>(qobj)) {
        if (!NIL_P(peer->qt6rb_self)) {
          rb_gc_unregister_address(&peer->qt6rb_self);
          peer->qt6rb_self = Qnil;
        }
      }
      qobj->deleteLater();
    }
    w->ptr = nullptr;
    w->owned = false;
  }
  return Qnil;
}

static VALUE qt_disposed_p(VALUE mod, VALUE obj) {
  (void)mod;
  if (NIL_P(obj)) return Qtrue;
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  return w->ptr ? Qfalse : Qtrue;
}

static VALUE s_constructable = Qnil;

VALUE constructable_module() {
  if (NIL_P(s_constructable)) {
    s_constructable = rb_define_module_under(module_qt(), "Constructable");
    rb_gc_register_address(&s_constructable);
    rb_define_method(s_constructable, "initialize",
                     RUBY_METHOD_FUNC(generic_initialize), -1);
  }
  return s_constructable;
}

void init_core(VALUE mQt) {
  // Registered first so it runs after all other end procs; marks the point
  // past which no shim or signal handler may call into Ruby
  rb_set_end_proc(mark_ruby_dead, Qnil);
  rb_define_module_function(mQt, "_dispose", RUBY_METHOD_FUNC(qt_dispose), 1);
  rb_define_module_function(mQt, "_disposed?", RUBY_METHOD_FUNC(qt_disposed_p), 1);
}

} // namespace qt6rb
