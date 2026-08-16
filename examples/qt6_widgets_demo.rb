ENV['QT_QPA_PLATFORM'] = 'offscreen'
require_relative File.join(__dir__, "../ext/qt6/qt6")

app = Qt::Application.new
puts "1. Application: #{app.class} < #{app.class.superclass}"

window = Qt::MainWindow.new
window.window_title = "COSMOS Qt6"
central = Qt::Widget.new
layout = Qt::VBoxLayout.new(central)

label  = Qt::Label.new("waiting...")
label.alignment = Qt::AlignCenter
edit   = Qt::LineEdit.new
edit.placeholder_text = "type here"
button = Qt::PushButton.new("Go")
combo  = Qt::ComboBox.new
combo.add_items(["alpha", "beta", "gamma"])          # QStringList
combo.add_item("delta", {"speed" => 42, "on" => true}) # QString + QVariant

[label, edit, button, combo].each { |w| layout.add_widget(w) }
window.central_widget = central
window.resize(Qt::Size.new(400, 200))                 # value class
window.show

puts "2. Hierarchy: #{Qt::PushButton.ancestors[0..4].inspect}"
sz = window.size
puts "3. QSize round-trip: #{sz.class} #{sz.width}x#{sz.height}"
geo = window.geometry
puts "4. QRect: #{geo.class} #{geo.width}x#{geo.height}, contains(1,1)=#{geo.contains(Qt::Point.new(1,1))}"
puts "5. Qt:: constants: AlignCenter=#{Qt::AlignCenter} Horizontal=#{Qt::Horizontal} Key_Enter=#{Qt::Key_Enter}"

clicked_arg = :none
button.on_clicked { |checked| clicked_arg = checked; label.text = "Hello #{edit.text}!" }

edit_signal = nil
edit.on_text_changed { |t| edit_signal = t }

timer = Qt::Timer.new
phase = 0
timer.on_timeout do
  phase += 1
  case phase
  when 1
    edit.text = "COSMOS"
    button.click
  when 2
    puts "6. clicked signal arg (bool): #{clicked_arg.inspect}"
    puts "7. label after click: #{label.text.inspect}"
    puts "8. text_changed signal arg: #{edit_signal.inspect}"
    combo.current_index = 3
    puts "9. combo QVariant data: #{combo.current_data.inspect}"
    puts "10. combo current_text: #{combo.current_text.inspect}"
    puts "11. checkbox tristate enum: #{Qt::CheckBox.instance_method(:check_state) ? 'present' : 'missing'}"
    app.quit
  end
end
timer.start(30)
app.exec
puts "DONE"
