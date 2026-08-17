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

    def resolve_method(receiver, name)
      return name if receiver.respond_to?(name)
      snake = Qt.underscore(name)
      return snake if receiver.respond_to?(snake)
      Kernel.raise NoMethodError, "no slot #{name} on #{receiver.class}"
    end

    def signal_connect(sender, signal, &block)
      name = signal[/\A(\w+)/, 1]
      meth = "on_#{Qt.underscore(name)}"
      unless sender.respond_to?(meth)
        Kernel.raise NoMethodError, "no signal #{name} (#{meth}) on #{sender.class}"
      end
      sender.send(meth, &block)
    end
  end

  constants.each do |const|
    klass = const_get(const)
    klass.include(WrapperExtensions) if klass.is_a?(Class)
  end

  # ---------------------------------------------------------------------
  # Main-thread execution (qtbindings RubyThreadFix equivalent)
  # ---------------------------------------------------------------------
  @@mt_queue = Queue.new
  @@mt_timer = nil

  # Must be called from the main (GUI) thread after the app exists
  def self.init_thread_fix
    return if @@mt_timer
    @@mt_timer = Qt::Timer.new
    @@mt_timer.on_timeout do
      until @@mt_queue.empty?
        block, done = @@mt_queue.pop(true)
        begin
          block.call
        rescue Exception => error
          STDERR.puts "Qt.execute_in_main_thread raised:\n#{error.message}\n#{error.backtrace.join("\n")}"
        ensure
          done << true if done
        end
      end
    end
    @@mt_timer.start(5)
  end

  def self.execute_in_main_thread(blocking = false, _sleep_period = nil, &block)
    if Thread.current == Thread.main
      return block.call
    end
    done = blocking ? Queue.new : nil
    @@mt_queue << [block, done]
    done.pop if done
    nil
  end
end

module Qt
  # qtbindings wrapped values in Qt::Variant; the new bindings marshal
  # QVariant natively, so Variant.new is a passthrough
  class Variant
    def self.new(value = nil)
      value
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

  def self.qVersion
    "6.0.0"
  end
end

# qtbindings-era code calls Variant#toSize/toPoint/etc on values read from
# QSettings; those now come back as the actual value classes
{ 'Size' => 'toSize', 'SizeF' => 'toSizeF', 'Point' => 'toPoint',
  'PointF' => 'toPointF', 'Rect' => 'toRect', 'RectF' => 'toRectF' }.each do |klass, meth|
  Qt.const_get(klass).class_eval do
    define_method(meth) { self }
    define_method(Qt.underscore(meth)) { self }
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
