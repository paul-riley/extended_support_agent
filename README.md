# Extended Support Agent

Run `extended_support_agent::publish_el7` in Puppet Enterprise 2026. Its required
input, `el7_extended_support_agent_location`, is the full path to the EL7 x86_64 Puppet agent RPM on the primary.
Supply this path when launching the plan. The task reads the build information from the RPM.

```sh
puppet plan run extended_support_agent::publish_el7 \
  el7_extended_support_agent_location=/path/to/el7-puppetagent.rpm
```

Set `puppet_enterprise_certname` to target a specific PE primary. Otherwise
the plan discovers the primary in PuppetDB using `is_pe` and
`pe_server_version`. The task verifies the target locally in either case.

Signature verification defaults to enabled. Set `require_gpg_check=false` to
publish an RPM without running RPM signature or digest verification.
The generated repo explicitly sets `gpgcheck=0` and the installer omits GPG key
handling. This variant gets a separate build directory so it can coexist with
the signature-checked variant.

The task requires root, Python 3, Bash, and `rpm`. It generates the Yum XML
metadata with Python’s standard library. Signature
verification also requires the signing key in the primary's RPM keyring.
Grant operators access to the plan and restrict the raw publishing task to
trusted administrators.

Module Hiera loads the EL7 data from `data/os/redhat_7.yaml`. It contains the
platform tag and installer and repo templates captured
from PE 2025.12.1. Those templates retain their Puppet Enterprise provenance.
The task reads the package endpoint and signing key from the current PE
installation when needed and renders the templates with the supplied RPM build. The
old release directory is only the source of the captured templates.

The active PE release receives a versioned `el-7-x86_64-<build>` directory
containing the RPM, Yum metadata, installer, and repo file. Three links
expose the standard PE paths beside the current bootstrap.

- `el-7-x86_64` selects the active build.
- `el-7-x86_64.bash` exposes its PE platform installer.
- `el-7-x86_64.repo` exposes its Yum repo configuration.

Selecting a new build changes the platform link. Previous build directories
remain available, and each installer pins its package to its versioned
repository. Running the plan again with the same active build is idempotent.
The task refuses to replace existing EL7 entry points it does not manage.

## Unit tests

Use PDK with Puppet 9, Python 3, and Bash.

```sh
pdk bundle install
pdk test unit
```

BoltSpec exercises plan targeting, Hiera lookup, and task arguments. The task
unit tests use temporary directories and stub Puppet and RPM. They generate and verify the repository
metadata. They run Bash syntax checks against the rendered installer.
