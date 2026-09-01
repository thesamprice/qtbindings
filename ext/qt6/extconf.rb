# Build the libclang-generated Qt 6 bindings.
#
# The generated glue is Qt-version-specific: each file binds exactly the API
# its Qt headers declared, so one generated against a newer Qt calls methods
# an older Qt has not added yet (QAbstractSpinBox::returnPressed, QImage::flip,
# QPalette::accent, ...) and fails to compile. generated/ therefore holds one
# file per Qt version; regenerate for yours with generator/regen.sh.
require 'mkmf'

qt_prefix = ENV['QT_PREFIX']
ENV['PKG_CONFIG_PATH'] = "#{qt_prefix}/lib/pkgconfig:#{ENV['PKG_CONFIG_PATH']}" if qt_prefix

$CXXFLAGS << ' -std=c++20 -fPIC'
# The runtime sources live next to this extconf in the repo layout
runtime_dir = File.expand_path('../../generator/runtime', __dir__)
generated_dir = File.join(__dir__, 'generated')
$INCFLAGS << " -I#{runtime_dir}"
$VPATH << runtime_dir << generated_dir

qt_version = `pkg-config --modversion Qt6Core`.strip
unless $?.success? && !qt_version.empty?
  abort 'Qt6Core.pc not found - install Qt 6 (apt install qt6-base-dev, ' \
        'brew install qt) or set QT_PREFIX'
end

# Newest generated file the local Qt is new enough to compile. A file
# generated against an older Qt only uses API the newer one still has, so
# falling back is safe; the reverse is not.
to_tuple = ->(v) { v.split(/[._]/).map(&:to_i) }
available = Dir.glob(File.join(generated_dir, 'qt6_generated_*.cpp')).map do |path|
  [to_tuple[File.basename(path)[/\Aqt6_generated_(.+)\.cpp\z/, 1]], path]
end.sort_by(&:first)
usable = available.select { |version, _| (version <=> to_tuple[qt_version]) <= 0 }
if usable.empty?
  have = available.map { |version, _| version.join('.') }
  have = have.empty? ? 'none' : have.join(', ')
  abort "No generated bindings usable on Qt #{qt_version} (have: #{have}). " \
        'Run generator/regen.sh to generate them for this Qt.'
end
generated_version, generated_path = usable.last
generated_version = generated_version.join('.')
if generated_version != qt_version
  warn "qtbindings: building the Qt #{generated_version} bindings against " \
       "Qt #{qt_version}. This is normal: Qt 6 keeps source compatibility, " \
       "so an older set still builds -- it just does not bind API added " \
       "after #{generated_version}. Run generator/regen.sh only if you " \
       'need that newer API.'
end
puts "Using #{File.basename(generated_path)} for Qt #{qt_version}"

$srcs = [generated_path] + Dir.glob("#{runtime_dir}/*.cpp")

abort 'Qt6Core.pc not found' unless pkg_config('Qt6Core')
# Optional GUI modules (their -DQT_*_LIB defines gate the widget classes)
pkg_config('Qt6Gui')
pkg_config('Qt6Widgets')

create_makefile('qt6')
