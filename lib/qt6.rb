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

class Qt::CoreApplication
  # exec/quit/processEvents are static in Qt 6; qtbindings-era code calls
  # them on the instance
  def exec; self.class.exec; end
  def quit; self.class.quit; end
  def process_events; self.class.process_events; end
  alias processEvents process_events

  alias qt6rb_original_initialize initialize
  def initialize(*args)
    qt6rb_original_initialize(*args)
    Qt.init_thread_fix
  end
end
