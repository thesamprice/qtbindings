ENV['QT_QPA_PLATFORM'] = 'offscreen'
require_relative File.join(__dir__, '../ext/qt6/qt6')

app = Qt::Application.new

# 1. QObject subclass hooking the protected timerEvent virtual
class Ticker < Qt::Object
  attr_reader :ticks, :event_classes
  def initialize
    super()
    @ticks = 0
    @event_classes = []
  end
  def timer_event(ev)
    @ticks += 1
    @event_classes << ev.class
  end
end

# 2. Widget subclass: public virtual (size_hint), protected virtual with
#    super (close_event), idiomatic initialize/super with a parent arg
class MyWidget < Qt::Widget
  attr_reader :closed, :hint_calls, :close_ev_class
  def initialize(parent = nil)
    super(parent)
    @closed = false
    @hint_calls = 0
  end
  def size_hint
    @hint_calls += 1
    Qt::Size.new(123, 45)
  end
  def close_event(ev)
    @closed = true
    @close_ev_class = ev.class
    ev.accept
    super(ev)   # calls QWidget::closeEvent via the base-caller binding
  end
end

# 3. Override that raises: must be reported, not crash the event loop
class Rowdy < Qt::Object
  def initialize; super(); end
  def timer_event(ev); raise "boom"; end
end

ticker = Ticker.new
tid = ticker.start_timer(15)

w = MyWidget.new
puts "1. subclass instance: #{w.class} (#{w.class.superclass}), object_name=#{w.object_name.inspect}"
w.show
w.adjust_size   # QWidget::adjustSize consults sizeHint -> shim -> Ruby
puts "2. size after adjust_size: #{w.size.width}x#{w.size.height} (Ruby size_hint called #{w.hint_calls}x from C++)"

plain = Qt::Widget.new
plain.adjust_size
puts "3. plain Qt::Widget still fine: #{plain.size.width}x#{plain.size.height}"

parented = MyWidget.new(w)
puts "4. initialize(parent) super: parent set? #{parented.parent.equal?(nil) ? 'no' : 'yes'}"

rowdy = Rowdy.new
rowdy_tid = rowdy.start_timer(15)

timer = Qt::Timer.new
timer.on_timeout do
  if ticker.ticks >= 3
    ticker.kill_timer(tid)
    rowdy.kill_timer(rowdy_tid)
    w.close
    app.quit
  end
end
timer.start(20)
app.exec

puts "5. timer_event hook: #{ticker.ticks} ticks, event arg classes: #{ticker.event_classes.uniq.inspect}"
puts "6. close_event hook: closed=#{w.closed}, event class: #{w.close_ev_class}"
puts "7. widget hidden after close (base closeEvent ran via super): visible=#{w.visible?}"
puts "DONE"
