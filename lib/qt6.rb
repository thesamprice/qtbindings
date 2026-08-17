# Ruby-side compatibility layer over the libclang-generated Qt 6 bindings.
# Restores the qtbindings-era API surface COSMOS and other qtbindings apps
# expect: SIGNAL()/connect idioms, Qt::red-style lowercase enums, dispose,
# app.exec instance methods, and Qt.execute_in_main_thread.

begin
  require 'qt6.bundle'
rescue LoadError
  require_relative '../ext/qt6/qt6'
end
require 'thread'

# qtbindings exposed SIGNAL()/SLOT() as global methods; they just tag strings
def SIGNAL(signature) signature end
def SLOT(signature) signature end

module Qt
  # qtbindings wrapped enums in Qt::Enum objects; the new bindings use plain
  # Integers. Kept so `x.is_a?(Qt::Enum)` checks still work.
  Enum = Integer

  # Convert a camelCase Qt name to the generated snake_case method name
  def self.underscore(name)
    name.to_s.gsub(/(.)([A-Z][a-z]+)/, '\1_\2').gsub(/([a-z0-9])([A-Z])/, '\1_\2').downcase
  end

  # Lowercase Qt namespace enums (Qt::red, Qt::black, ...) can't be Ruby
  # constants; serve them via method_missing from the generated hash
  def self.method_missing(name, *args)
    value = LOWERCASE_ENUMS[name]
    return value if value and args.empty?
    super
  end

  def self.respond_to_missing?(name, include_private = false)
    LOWERCASE_ENUMS.key?(name) or super
  end

  # Common behavior for every wrapped Qt object
  module WrapperExtensions
    def self.included(base)
      base.extend(ClassMethods)
    end

    # qtbindings class-level declarations. Slots need no declaration with
    # the new bindings (connect dispatches by method name); accept and
    # ignore them.
    module ClassMethods
      def slots(*_signatures); end

      # qtbindings constructor blocks. A block that takes no argument is
      # instance_eval'd against the new object:
      #   Qt::PushButton.new('Ok') { connect(SIGNAL('clicked()')) { ... } }
      # one that takes an argument is called with it:
      #   Qt::Dialog.new(self) { |dialog| ... dialog.exec }
      def new(*args, &block)
        obj = super(*args, &nil)
        if block
          block.arity <= 0 ? obj.instance_eval(&block) : block.call(obj)
        end
        obj
      end

      # qtbindings exposed scoped enums as class *methods* as well as
      # constants, so both Qt::Style::CE_PushButton and
      # Qt::Style.CE_ItemViewItem worked. Constants declared on this class or
      # a Qt ancestor (never Object's globals) answer the method form too.
      def method_missing(name, *args, &block)
        if args.empty? && !block && name.to_s.match?(/\A[A-Z]/)
          ancestors.each do |mod|
            break if mod == Object
            return mod.const_get(name, false) if mod.const_defined?(name, false)
          end
        end
        super
      end

      def respond_to_missing?(name, include_private = false)
        if name.to_s.match?(/\A[A-Z]/)
          ancestors.each do |mod|
            break if mod == Object
            return true if mod.const_defined?(name, false)
          end
        end
        super
      end

      # Ruby-defined signals: `signals 'modified(int)'` defines a method
      # `modified` which invokes every connected handler (the qtruby
      # convention: `emit modified(5)` calls the signal method, and emit
      # itself is a pass-through). Handlers connect through the usual
      # connect(SIGNAL('modified(int)')) forms via the generated
      # on_<name> registration method. Cross-thread emissions are queued
      # to the main thread like Qt's AutoConnection.
      def signals(*signatures)
        signatures.each do |signature|
          name = signature[/\A(\w+)/, 1]
          snake = Qt.underscore(name)
          define_method(name) do |*args|
            handlers = @__qt6rb_signal_handlers && @__qt6rb_signal_handlers[name]
            if handlers
              handlers.each do |handler|
                arity = handler.arity
                arity = args.length if arity < 0
                if Thread.current == Thread.main
                  handler.call(*args[0, arity])
                else
                  Qt.execute_in_main_thread(false) { handler.call(*args[0, arity]) }
                end
              end
            end
            nil
          end
          alias_method(snake, name) if snake != name
          define_method("on_#{snake}") do |&block|
            @__qt6rb_signal_handlers ||= {}
            (@__qt6rb_signal_handlers[name] ||= []) << block
            self
          end
        end
      end
    end

    # qtruby's emit is a pass-through: the signal method call inside the
    # emit expression performs the emission
    def emit(result = nil)
      result
    end

    # qtbindings dispatched every Qt call through a public Qt::Base#method_missing,
    # and app code took advantage of that to force dispatch past a Ruby-level
    # override (e.g. `@timer.method_missing(:start, 100)`). The generated
    # bindings define real methods, so provide a public forwarder that behaves
    # like ordinary dispatch (and still raises NoMethodError for real typos).
    def method_missing(name, *args, &block)
      # Only reachable explicitly when the method exists (implicit dispatch
      # never lands here for a defined method) -- except for a `super` call
      # in a reopened class over a method the bindings define directly, which
      # Ruby also routes here. Re-sending would loop forever, so detect the
      # re-entry and report the real problem.
      active = (Thread.current[:qt6rb_method_missing] ||= {})
      key = [object_id, name]
      if active[key]
        Kernel.raise NoMethodError,
          "no superclass method '#{name}' for #{self.class}: the Qt 6 bindings " \
          "define it on this class, so a reopened `def #{name}; super; end` has " \
          "nothing to call -- alias the original method instead"
      end
      active[key] = true
      begin
        # qtbindings' method_missing dispatched straight into the C++ method
        # table, so an explicit call reached the *Qt* method even when a Ruby
        # subclass defined one of the same name. COSMOS relies on that:
        # LineGraph#graph is `method_missing(:update)` because
        # LinegraphPlotGuiObject defines its own #update(redraw_needed) and
        # QWidget::update is still what graph() wants (plain __send__ here
        # recurses update -> auto_scale_y -> graph -> update). Prefer the
        # generated implementation, then fall back to ordinary dispatch.
        meth = qt6rb_binding_method(name)
        snake = Qt.underscore(name)
        meth ||= qt6rb_binding_method(snake.to_sym) if snake != name.to_s
        return meth.bind(self).call(*args, &block) if meth

        return __send__(name, *args, &block) if respond_to?(name)
        return __send__(snake, *args, &block) if snake != name.to_s && respond_to?(snake)
      ensure
        active.delete(key)
      end
      super
    end
    public :method_missing

    # The nearest ancestor implementation of +name+ that the bindings define
    # in C (Ruby-defined methods report a source_location, generated ones do
    # not), or nil when every implementation is Ruby-level.
    def qt6rb_binding_method(name)
      self.class.ancestors.each do |mod|
        next unless mod.method_defined?(name, false) ||
                    mod.private_method_defined?(name, false)
        meth = mod.instance_method(name)
        return meth if meth.source_location.nil?
      end
      nil
    end
    private :qt6rb_binding_method

    def dispose
      Qt._dispose(self)
    end

    def disposed?
      Qt._disposed?(self)
    end

    # qtbindings-style connect:
    #   connect(SIGNAL('clicked()')) { ... }
    #   connect(sender, SIGNAL('clicked()'), receiver, SLOT('doIt()'))
    #   connect(sender, SIGNAL('clicked()')) { ... }
    def connect(*args, &block)
      case args.length
      when 1
        signal_connect(self, args[0], &block)
      when 2
        signal_connect(args[0], args[1], &block)
      when 4
        sender, signal, receiver, slot = args
        slot_name = slot[/\A(\w+)/, 1]
        signal_connect(sender, signal) do |*sig_args|
          meth = receiver.method(resolve_method(receiver, slot_name))
          arity = meth.arity
          arity = sig_args.length if arity < 0
          meth.call(*sig_args[0, arity])
        end
      else
        Kernel.raise ArgumentError, "connect expects 1, 2, or 4 arguments (got #{args.length})"
      end
    end

    private

    # Qt slots are frequently declared under `protected` in qtbindings-era
    # classes (OverviewTabbedPlots#handle_tab_change and friends), so the
    # lookup has to see non-public methods -- Object#method can call them.
    def resolve_method(receiver, name)
      return name if receiver.respond_to?(name, true)
      snake = Qt.underscore(name)
      return snake if receiver.respond_to?(snake, true)
      Kernel.raise NoMethodError, "no slot #{name} on #{receiver.class}"
    end

    def signal_connect(sender, signal, &block)
      name = signal[/\A(\w+)/, 1]
      meth = "on_#{Qt.underscore(name)}"
      unless sender.respond_to?(meth)
        Kernel.raise NoMethodError, "no signal #{name} (#{meth}) on #{sender.class}"
      end
      # Generated registrations take the full signature so overloaded signals
      # (QCompleter#activated) pick the right overload. Ruby-defined signals
      # (WrapperExtensions::ClassMethods#signals) take the block only.
      if sender.method(meth).arity == 0
        sender.send(meth, &block)
      else
        sender.send(meth, signal, &block)
      end
    end
  end

  constants.each do |const|
    klass = const_get(const)
    klass.include(WrapperExtensions) if klass.is_a?(Class)
  end

  # ---------------------------------------------------------------------
  # Main-thread execution (qtbindings RubyThreadFix equivalent)
  # ---------------------------------------------------------------------
  # The queue holds plain callables so qtbindings-era code that drains it by
  # hand (Qt::RubyThreadFix.queue.pop.call until ...empty?) keeps working.
  @@mt_queue = Queue.new
  @@mt_timer = nil

  # The shared main-thread callback queue
  def self.main_thread_queue
    @@mt_queue
  end

  # Must be called from the main (GUI) thread after the app exists
  def self.init_thread_fix
    return if @@mt_timer
    @@mt_timer = Qt::Timer.new
    @@mt_timer.on_timeout do
      @@mt_queue.pop.call until @@mt_queue.empty?
    end
    @@mt_timer.start(1)
  end

  # Code which accesses the GUI must run in the main (GUI) thread. Signature
  # and defaults match qtbindings' Qt.execute_in_main_thread.
  #
  # @param blocking [Boolean] Block the calling thread until the block has run
  # @param sleep_period [Float] Poll interval used while blocking
  # @param delay_execution [Boolean] Only consulted when already on the main
  #   thread: queue the block instead of running it inline. Callers rely on
  #   this to break re-entrancy (e.g. PacketViewer retrying update_tlm_items
  #   while its telemetry thread shuts down), so it must never run inline.
  def self.execute_in_main_thread(blocking = true, sleep_period = 0.001, delay_execution = false, &block)
    if Thread.current != Thread.main
      complete = false
      @@mt_queue << lambda do
        begin
          block.call
        rescue Exception => error
          STDERR.puts "Qt.execute_in_main_thread raised:\n#{error.message}\n#{error.backtrace.join("\n")}"
        ensure
          complete = true
        end
      end
      sleep(sleep_period) until complete if blocking
      nil
    elsif delay_execution
      @@mt_queue << lambda do
        begin
          block.call
        rescue Exception => error
          STDERR.puts "Qt.execute_in_main_thread raised:\n#{error.message}\n#{error.backtrace.join("\n")}"
        end
      end
      nil
    else
      block.call
    end
  end

  # qtbindings exposed the main-thread pump as a Qt::Object subclass; COSMOS
  # only ever touches RubyThreadFix.queue to drain it before disposing a
  # dialog, so a bare shim over the shared queue is enough.
  class RubyThreadFix
    def self.queue
      Qt.main_thread_queue
    end
  end
end

module Qt
  # Qt::ItemIsTristate was the Qt 4 name for the item flag that Qt 5.6
  # renamed to Qt::ItemIsAutoTristate; the old spelling stayed on as a
  # deprecated alias for the same value (64) until Qt 6 dropped it. Note it is
  # NOT Qt::ItemIsUserTristate, which is a different flag (256) that Qt 5.6
  # added at the same time -- resolving the old name to that one would change
  # behaviour rather than preserve it. qtbindings-era code (Cosmos
  # TableManager) uses the Qt 4 spelling.
  ItemIsTristate = ItemIsAutoTristate

  # qtbindings wrapped values in Qt::Variant; the new bindings marshal
  # QVariant natively, so Variant.new is a passthrough
  class Variant
    def self.new(value = nil)
      value
    end
  end

  # Mutable out-parameter for Qt's `bool *ok` arguments
  # (Qt::InputDialog.getText(..., qt_boolean)). The generated bindings write
  # the flag back through #value=. qtbindings' quirk of reporting an unset /
  # false flag as #nil? is preserved: COSMOS tests `qt_boolean.nil?` to detect
  # a cancelled dialog.
  class Boolean
    attr_accessor :value

    def initialize(value = nil)
      @value = value
    end

    def nil?
      !@value
    end

    def to_s
      @value.to_s
    end

    def inspect
      "#<Qt::Boolean #{@value.inspect}>"
    end
  end

  # QDesktopWidget was removed in Qt 6; emulate the small surface COSMOS
  # uses on top of QScreen
  class DesktopCompat
    ScreenSize = Struct.new(:width, :height)

    def screen(_index = nil)
      geometry = Qt::GuiApplication.primaryScreen.geometry
      # QDesktopWidget#screen returned a widget; callers use width/height
      ScreenSize.new(geometry.width, geometry.height)
    end

    def screenGeometry(_index = nil)
      Qt::GuiApplication.primaryScreen.geometry
    end
    alias screen_geometry screenGeometry

    def availableGeometry(_index = nil)
      Qt::GuiApplication.primaryScreen.availableGeometry
    end
    alias available_geometry availableGeometry

    def width; screenGeometry.width; end
    def height; screenGeometry.height; end
  end

  # QSound was removed in Qt 6 (Qt 5 deprecated it in favour of
  # QtMultimedia's QSoundEffect, which these bindings do not wrap -- we
  # generate against QtCore/QtGui/QtWidgets only). COSMOS calls
  # Cosmos.play_wav_file for dialog chimes, which is guarded by
  # `Qt::Sound.isAvailable`; report "no audio device" so the guard short
  # circuits and the chime is silently skipped rather than raising NameError.
  class Sound
    def self.isAvailable; false; end
    class << self; alias_method :is_available, :isAvailable; end
    def self.available?; false; end

    # Never reached while isAvailable is false, but keep the surface complete
    # so a caller that skips the guard degrades to a no-op instead of raising.
    def self.play(_filename = nil); nil; end
  end

  def self.qVersion
    "6.0.0"
  end

  # Qt 5 folded QStyleOptionViewItemV2/V3/V4 (and the ...ButtonV2 variants)
  # back into their base classes; the versioned names live on in
  # qtbindings-era item delegates.
  StyleOptionViewItemV2 = StyleOptionViewItem
  StyleOptionViewItemV3 = StyleOptionViewItem
  StyleOptionViewItemV4 = StyleOptionViewItem

  # qtbindings returned every QVariant as a Qt::Variant with toXxx accessors.
  # QVariant is now marshalled to plain Ruby values (Qt::Variant.new is a
  # passthrough), so give those values the same accessors. Scoped to the
  # classes from_qvariant can produce rather than added to Object, where
  # #value in particular would shadow application methods.
  module VariantValue
    def toString; to_s; end
    def toInt; to_i; end
    def toFloat; to_f; end
    def toDouble; to_f; end
    def toBool; self ? true : false; end
    def toStringList; Array(self).map(&:to_s); end
    def toVariant; self; end
    # Qt::Variant#value returned the wrapped Ruby object
    def value; self; end
    def isValid; !nil?; end
    def isNull; nil?; end
    # Ruby's implicit conversion protocol (to_int, to_str, to_ary, ...) must
    # not be touched: a String answering #to_int with an Integer would make
    # File.open(path, "w+") read the mode as an integer flag.
    IMPLICIT_CONVERSIONS = %w(to_int to_str to_ary to_hash to_a to_io to_proc)
    %w(toString toInt toFloat toDouble toBool toStringList toVariant
       isValid isNull).each do |m|
      snake = Qt.underscore(m)
      next if IMPLICIT_CONVERSIONS.include?(snake)
      alias_method(snake, m)
    end
  end
  # qtbindings rooted every wrapper at Qt::Base, so Qt::Widget's superclass
  # was Qt::Base. COSMOS's line_graph C extension hard-codes that hierarchy
  # (rb_define_class_under(mQt, "Base", rb_cObject) then
  # rb_define_class_under(mQt, "Widget", cQtBase)) before subclassing
  # Qt::Widget, which raises "superclass mismatch" unless Qt::Base resolves
  # to the real parent of Qt::Widget. Qt::Object is that parent and it
  # descends directly from Object, so both re-declarations line up.
  Base = Object
end

[String, Integer, Float, Symbol, Array, Hash, TrueClass, FalseClass, NilClass]
  .each { |k| k.include(Qt::VariantValue) }

# qtbindings-era code calls Variant#toSize/toPoint/etc on values read from
# QSettings; those now come back as the actual value classes
{ 'Size' => 'toSize', 'SizeF' => 'toSizeF', 'Point' => 'toPoint',
  'PointF' => 'toPointF', 'Rect' => 'toRect', 'RectF' => 'toRectF' }.each do |klass, meth|
  Qt.const_get(klass).class_eval do
    define_method(meth) { self }
    define_method(Qt.underscore(meth)) { self }
  end
end

# QPolygon(int size) came from QVector in Qt 4/5. In Qt 6 QPolygon inherits
# QList<QPoint>'s constructors, and the generator cannot see through that
# template base, so the sized constructor is missing. putPoints() grows the
# polygon, which is all the sized constructor was ever used for.
class Qt::Polygon
  def initialize(arg = nil)
    if arg.is_a?(Integer)
      super()
      arg.times { |i| putPoints(i, 1, 0, 0) }
    elsif arg.nil?
      super()
    else
      super(arg)
    end
  end
end

# Qt 6.9 removed every integral QChar constructor (QT_CORE_REMOVED_SINCE) in
# favour of template ones, which libclang reports as templates rather than
# concrete overloads, so the generator cannot bind them. qtbindings-era code
# says Qt::Char.new(57), so rebuild that on the one integral constructor that
# survived: QChar(uchar cell, uchar row), composing (row << 8) | cell. QChar's
# copy constructor is implicit, so libclang emits no cursor for it either --
# copy by code point instead.
class Qt::Char
  def initialize(arg = nil, row = nil)
    return super(arg, row) if row

    code =
      case arg
      when nil then return super()
      when Integer then arg
      when Qt::Char then arg.unicode
      when String
        raise ArgumentError, "Qt::Char.new expects a single character" if arg.length != 1
        arg.ord
      else raise TypeError, "cannot make a Qt::Char from #{arg.class}"
      end
    raise RangeError, "#{code} is outside the basic multilingual plane" unless (0..0xFFFF).cover?(code)
    super(code & 0xFF, (code >> 8) & 0xFF)
  end
end

# QDialog::exec() runs Qt's nested event loop inside a single C call, and that
# call holds the Ruby GVL for the entire lifetime of the dialog: the poll it
# blocks in never returns to the Ruby VM, so no other Ruby thread is ever
# scheduled. That deadlocks any dialog whose completion depends on a Ruby
# thread -- Cosmos::ProgressDialog runs its work on one and closes itself from
# there, and every Qt.execute_in_main_thread(blocking) caller sleeps on one.
# Driving the loop from Ruby is equivalent (modality is a window property, not
# something exec confers) and releases the GVL each pass. See the body for how
# that property has to be set for the dialog to really become modal.
# The alias is deliberately NOT called qt6_exec: that name belongs to app code
# reopening Qt::Dialog (Cosmos does exactly that in
# gui/utilities/script_module_gui.rb), and squatting it would make that
# reopen's `unless method_defined?(:qt6_exec)` guard skip its own alias, so it
# would call straight past this replacement into the blocking C++ exec.
class Qt::Dialog
  unless method_defined?(:qt6rb_native_exec)
    alias_method :qt6rb_native_exec, :exec if method_defined?(:exec)

    def exec
      # C++ QDialog::exec() makes the dialog modal by setting Qt::WA_ShowModal,
      # which is exactly what #setModal does -- but setting that attribute on
      # its own is not enough to make a dialog that HAS A PARENT actually
      # modal. QWidget only pushes window_modality down to the underlying
      # QWindow when Qt::WA_SetWindowModality is also set, and that attribute
      # is set by setWindowModality, never by setModal. A parented dialog
      # shown after only setModal(true) therefore reports isModal == true and
      # windowModality == ApplicationModal while never entering Qt's modal
      # window list: it does not block its parent, and
      # QApplication::activeModalWidget cannot see it. Going through
      # setWindowModality sets both, so honour any modality the dialog already
      # asked for and default to ApplicationModal as QDialog::exec does.
      requested = windowModality
      requested = Qt::ApplicationModal if requested == Qt::NonModal
      setWindowModality(requested)
      show
      # done()/accept()/reject() all hide the dialog and set its result
      while isVisible
        Qt::CoreApplication.process_events
        # Drain directly rather than relying on the init_thread_fix timer:
        # that timer's handler is already on the stack (a queued block is what
        # usually opens a dialog), and a nested event loop must not depend on
        # it being re-entered.
        Qt.main_thread_queue.pop.call until Qt.main_thread_queue.empty?
        sleep 0.001
      end
      result
    end
  end
end

# QFontMetrics::width() was deprecated in Qt 5.11 and removed in Qt 6 in
# favour of horizontalAdvance().
class Qt::FontMetrics
  alias_method :width, :horizontalAdvance if method_defined?(:horizontalAdvance)
end

# Qt 5 renamed QHeaderView's per-section accessors; qtbindings-era code uses
# the Qt 4 spellings.
class Qt::HeaderView
  { 'setResizeMode' => 'setSectionResizeMode',
    'resizeMode' => 'sectionResizeMode',
    'setMovable' => 'setSectionsMovable',
    'isMovable' => 'sectionsMovable',
    'setClickable' => 'setSectionsClickable',
    'isClickable' => 'sectionsClickable' }.each do |old, new|
    next unless method_defined?(new)
    alias_method(old, new)
    alias_method(Qt.underscore(old), new)
  end
end

class Qt::Application
  def self.desktop
    @desktop_compat ||= Qt::DesktopCompat.new
  end
end

class Qt::CoreApplication
  # exec/quit/processEvents are static in Qt 6; qtbindings-era code calls
  # them on the instance
  def exec; self.class.exec; end
  def quit; self.class.quit; end
  def process_events; self.class.process_events; end
  alias processEvents process_events

  # activeWindow is a QApplication static in Qt 6; qtbindings-era code
  # calls it on the instance
  def activeWindow
    Qt::Application.activeWindow
  end
  alias active_window activeWindow

  alias qt6rb_original_initialize initialize
  def initialize(*args)
    qt6rb_original_initialize(*args)
    Qt.init_thread_fix
  end
end
