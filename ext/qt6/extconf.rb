# Build the libclang-generated Qt 6 bindings prototype.
#
# Regenerate the glue first (see generator/README.md):
#   generator-venv/bin/python generator/codegen.py \
#     --qt-prefix /opt/homebrew/opt/qt --modules QtCore \
#     --classes QObject QTimer -o ext/qt6/qt6_generated.cpp
require 'mkmf'

qt_prefix = ENV['QT_PREFIX'] || '/opt/homebrew/opt/qt'
ENV['PKG_CONFIG_PATH'] = "#{qt_prefix}/lib/pkgconfig:#{ENV['PKG_CONFIG_PATH']}"

$CXXFLAGS << ' -std=c++20 -fPIC'
# The runtime sources live next to this extconf in the repo layout
runtime_dir = File.expand_path('../../generator/runtime', __dir__)
$INCFLAGS << " -I#{runtime_dir}"
$srcs = Dir.glob("#{__dir__}/*.cpp") + Dir.glob("#{runtime_dir}/*.cpp")
$VPATH << runtime_dir

unless pkg_config('Qt6Core')
  abort 'Qt6Core.pc not found - install Qt 6 (brew install qt) or set QT_PREFIX'
end

create_makefile('qt6')
