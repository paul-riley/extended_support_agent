# Publish a staged EL7 Puppet agent build on the PE primary discovered in PuppetDB.
# @param el7_extended_support_agent_location EL7 Extended Support Agent Location. Absolute path to the RPM on the PE primary.
# @param require_gpg_check Verify the RPM signature on the primary and require Yum GPG checks.
# @param puppet_enterprise_certname Target certname. Omit to discover the PE primary in PuppetDB.
plan extended_support_agent::publish_el7(
  Pattern[/\A\/.+\.rpm\z/] $el7_extended_support_agent_location,
  Boolean $require_gpg_check = true,
  Optional[Pattern[/\A[A-Za-z0-9][A-Za-z0-9._-]*\z/]] $puppet_enterprise_certname = undef,
) {
  $target = if $puppet_enterprise_certname == undef {
    $query = @(QUERY)
      inventory[certname] {
        facts.is_pe = true and facts.pe_server_version ~ "^2026[.]"
      }
      | QUERY
    $primaries = puppetdb_query($query)
    if $primaries.length != 1 {
      fail_plan("Expected one PE 2026 primary in PuppetDB, found ${primaries.length}.")
    }
    $primaries[0]['certname']
  } else {
    $puppet_enterprise_certname
  }
  $arguments = {
    'el7_extended_support_agent_location' => $el7_extended_support_agent_location,
    'require_gpg_check'                   => $require_gpg_check,
    'current_package_path'                => lookup('extended_support_agent::current_package_path', String[1]),
    'platform_tag'                        => lookup('extended_support_agent::el7_platform_tag', String[1]),
    'repo_template'                       => lookup('extended_support_agent::el7_repo_template', String[1]),
    'installer_template'                  => lookup('extended_support_agent::el7_installer_template', String[1]),
  }
  return(run_task('extended_support_agent::publish_el7', $target, $arguments))
}
