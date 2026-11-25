# frozen_string_literal: true

require 'json'
require 'open3'
require 'yaml'

describe 'publish_el7 Python task' do
  it 'passes the publisher unit tests with the module Hiera templates' do
    templates = YAML.safe_load_file(File.expand_path('../../data/os/redhat_7.yaml', __dir__))
    output, status = Open3.capture2e(
      { 'PYTHONDONTWRITEBYTECODE' => '1' }, 'python3', File.join(__dir__, 'publish_el7_test.py'),
      stdin_data: JSON.generate(templates)
    )

    expect(status.success?).to be(true), output
  end
end
