# frozen_string_literal: true

require 'spec_helper'
require 'yaml'

describe 'extended_support_agent::publish_el7' do
  include BoltSpec::Plans

  let(:certname) { 'primary.example.test' }
  let(:rpm_path) { '/etc/el7-puppetagent.rpm' }
  let(:module_root) { File.expand_path('../..', __dir__) }
  let(:el7) { YAML.safe_load_file(File.join(module_root, 'data/os/redhat_7.yaml')) }
  let(:common) { YAML.safe_load_file(File.join(module_root, 'data/common.yaml')) }
  let(:plan_name) { 'extended_support_agent::publish_el7' }

  def task_params(gpg_check)
    {
      'el7_extended_support_agent_location' => rpm_path,
      'require_gpg_check' => gpg_check,
      'current_package_path' => common.fetch('extended_support_agent::current_package_path'),
      'platform_tag' => el7.fetch('extended_support_agent::el7_platform_tag'),
      'repo_template' => el7.fetch('extended_support_agent::el7_repo_template'),
      'installer_template' => el7.fetch('extended_support_agent::el7_installer_template'),
    }
  end

  it 'discovers a PE 2026 primary and passes Hiera content with GPG checks enabled' do
    expect(puppetdb_client).to receive(:make_query).with(/facts\.is_pe = true.*facts\.pe_server_version/m, nil, nil).and_return([{ 'certname' => certname }])
    expect_task(plan_name).with_targets([certname]).with_params(task_params(true))

    result = run_plan(plan_name, 'el7_extended_support_agent_location' => rpm_path)

    expect(result.ok?).to be(true), result.value.to_s
  end

  it 'skips discovery for an explicit certname and passes through false' do
    expect(puppetdb_client).not_to receive(:make_query)
    expect_task(plan_name).with_targets([certname]).with_params(task_params(false))

    result = run_plan(plan_name, {
      'el7_extended_support_agent_location' => rpm_path,
      'puppet_enterprise_certname' => certname,
      'require_gpg_check' => false,
    })

    expect(result.ok?).to be(true), result.value.to_s
  end

  it 'fails before dispatching when discovery is ambiguous' do
    expect(puppetdb_client).to receive(:make_query).and_return([{ 'certname' => certname }, { 'certname' => 'other.example.test' }])
    expect_task(plan_name).not_be_called

    result = run_plan(plan_name, 'el7_extended_support_agent_location' => rpm_path)

    expect(result.ok?).to be(false)
    expect(result.value.to_s).to include('Expected one PE 2026 primary')
  end
end
