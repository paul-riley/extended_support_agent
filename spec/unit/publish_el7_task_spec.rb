# frozen_string_literal: true

require 'json'
require 'open3'
require 'yaml'

describe 'publish_el7 Python task' do
  let(:templates) { YAML.safe_load_file(File.expand_path('../../data/os/redhat_7.yaml', __dir__)) }

  def run_task_tests
    Open3.capture2e(
      { 'PYTHONDONTWRITEBYTECODE' => '1' }, 'python3', File.join(__dir__, 'publish_el7_test.py'),
      stdin_data: JSON.generate(templates)
    )
  end

  it 'passes the publisher unit tests with the module Hiera templates' do
    output, status = run_task_tests
    expect(status.success?).to be(true), output
  end
end
