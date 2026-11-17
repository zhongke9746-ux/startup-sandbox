#!/usr/bin/env ruby
# 把 rules/*.yaml 编译成 build/*.json，供 Python 引擎读取（环境无 python-yaml）
require 'yaml'
require 'json'
require 'fileutils'

root = File.expand_path('..', __dir__)
src  = File.join(root, 'rules')
out  = File.join(root, 'build')
FileUtils.mkdir_p(out)

Dir.glob(File.join(src, '*.yaml')).sort.each do |f|
  begin
    data = YAML.load_file(f)
    base = File.basename(f, '.yaml')
    File.write(File.join(out, "#{base}.json"), JSON.pretty_generate(data))
    puts "OK  #{base}.yaml -> build/#{base}.json"
  rescue => e
    puts "ERR #{File.basename(f)}: #{e.message}"
    exit 1
  end
end
