# frozen_string_literal: true

require 'spec_helper'
require 'bolt_spec/plans'
require 'yaml'

BoltSpec::Plans.init

describe 'extended_support_agent::publish_el7' do
  include BoltSpec::Plans

  let(:certname) { 'compiler.example.test' }
  let(:rpm_path) { '/etc/el7-puppetagent.rpm' }
  let(:el7) { YAML.safe_load_file(File.join(module_root, 'data/os/redhat_7.yaml')) }
  let(:common) { YAML.safe_load_file(File.join(module_root, 'data/common.yaml')) }
  let(:plan_name) { 'extended_support_agent::publish_el7' }

  def module_root
    File.expand_path('../..', __dir__)
  end

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

  it 'discovers a PE 2026 server and passes Hiera content with GPG checks enabled' do
    allow(puppetdb_client).to receive(:make_query).with(%r{facts\.is_pe = true.*facts\.pe_server_version}m, nil, nil).and_return([{ 'certname' => certname }])
    expect_task(plan_name).with_targets([certname]).with_params(task_params(true))

    result = run_plan(plan_name, 'el7_extended_support_agent_location' => rpm_path)

    expect(result.ok?).to be(true), result.value.to_s
  end

  def explicit_params
    {
      'el7_extended_support_agent_location' => rpm_path,
      'puppet_enterprise_certname' => certname,
      'require_gpg_check' => false,
    }
  end

  it 'skips discovery for an explicit compiler certname and passes through false' do
    allow(puppetdb_client).to receive(:make_query).and_raise('An explicit target must skip discovery')
    expect_task(plan_name).with_targets([certname]).with_params(task_params(false))

    result = run_plan(plan_name, explicit_params)

    expect(result.ok?).to be(true), result.value.to_s
  end

  it 'fails before dispatching when discovery is ambiguous' do
    allow(puppetdb_client).to receive(:make_query).and_return([{ 'certname' => certname }, { 'certname' => 'other.example.test' }])
    expect_task(plan_name).not_be_called

    result = run_plan(plan_name, 'el7_extended_support_agent_location' => rpm_path)

    expect(result).to have_attributes(ok?: false, value: satisfy { |value| value.to_s.include?('Expected one PE 2026 server') })
  end
end
