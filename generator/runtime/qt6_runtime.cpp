#include "qt6_runtime.hpp"
#include <vector>
#include <cstring>

namespace qt6rb {

static VALUE s_module_qt = Qnil;
static VALUE s_proc_registry = Qnil;

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

VALUE define_class(ClassInfo* info, const char* name, VALUE superclass) {
  VALUE super = NIL_P(superclass) ? rb_cObject : superclass;
  info->rb_class = rb_define_class_under(module_qt(), name, super);
  rb_gc_register_address(&info->rb_class);
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

void* unwrap(VALUE obj, ClassInfo* cls) {
  if (NIL_P(obj)) return nullptr;
  Wrapper* w;
  TypedData_Get_Struct(obj, Wrapper, &wrapper_type, w);
  if (cls && !rb_obj_is_kind_of(obj, cls->rb_class)) {
    rb_raise(rb_eTypeError, "expected %s but got %s",
             cls->cxx_name, rb_obj_classname(obj));
  }
  return w->ptr;
}

VALUE wrap_qobject(QObject* obj, ClassInfo* cls) {
  if (!obj) return Qnil;
  return wrap(obj, cls, obj->parent() == nullptr);
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

void retain_proc(VALUE proc) {
  if (NIL_P(s_proc_registry)) {
    s_proc_registry = rb_ary_new();
    rb_gc_register_address(&s_proc_registry);
  }
  rb_ary_push(s_proc_registry, proc);
}

struct ProcCall { VALUE proc; int argc; const VALUE* argv; };

static VALUE call_proc_body(VALUE arg) {
  ProcCall* pc = reinterpret_cast<ProcCall*>(arg);
  return rb_funcallv(pc->proc, rb_intern("call"), pc->argc, pc->argv);
}

VALUE call_proc(VALUE proc, int argc, const VALUE* argv) {
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

// ---------------------------------------------------------------------------
// Hand-written QCoreApplication
// ---------------------------------------------------------------------------

static ClassInfo coreapp_class = { "QCoreApplication", Qnil, nullptr };

// QCoreApplication requires argc/argv that outlive it
struct AppArgs {
  int argc = 0;
  std::vector<char*> argv;
  std::vector<std::string> storage;
};
static AppArgs s_app_args;

static VALUE coreapp_new(int argc, VALUE* argv, VALUE klass) {
  VALUE rb_args = Qnil;
  rb_scan_args(argc, argv, "01", &rb_args);

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
  for (auto& s : s_app_args.storage) s_app_args.argv.push_back(s.data());
  s_app_args.argc = (int)s_app_args.argv.size();

  QCoreApplication* app = new QCoreApplication(s_app_args.argc, s_app_args.argv.data());
  return wrap(app, &coreapp_class, true);
}

static VALUE coreapp_exec(VALUE self) {
  (void)self;
  return INT2NUM(QCoreApplication::exec());
}

static VALUE coreapp_quit(VALUE self) {
  (void)self;
  QCoreApplication::quit();
  return Qnil;
}

static VALUE coreapp_process_events(VALUE self) {
  (void)self;
  QCoreApplication::processEvents();
  return Qnil;
}

static VALUE coreapp_application_name(VALUE self) {
  (void)self;
  return from_qstring(QCoreApplication::applicationName());
}

static VALUE coreapp_set_application_name(VALUE self, VALUE name) {
  (void)self;
  QCoreApplication::setApplicationName(to_qstring(name));
  return name;
}

void init_core(VALUE mQt) {
  (void)mQt;
  VALUE cls = define_class(&coreapp_class, "CoreApplication", Qnil);
  rb_undef_alloc_func(cls);
  rb_define_singleton_method(cls, "new", RUBY_METHOD_FUNC(coreapp_new), -1);
  rb_define_method(cls, "exec", RUBY_METHOD_FUNC(coreapp_exec), 0);
  rb_define_method(cls, "quit", RUBY_METHOD_FUNC(coreapp_quit), 0);
  rb_define_method(cls, "process_events", RUBY_METHOD_FUNC(coreapp_process_events), 0);
  rb_define_method(cls, "application_name", RUBY_METHOD_FUNC(coreapp_application_name), 0);
  rb_define_method(cls, "application_name=", RUBY_METHOD_FUNC(coreapp_set_application_name), 1);
}

} // namespace qt6rb
