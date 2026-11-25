#!/usr/bin/python3
"""Publish and activate a versioned EL7 installer on the PE primary."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def command(*args):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise ValueError("{} failed: {}".format(args[0], result.stdout.strip()))
    return result.stdout.strip()


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def render_installers(
    release_root, build, package_spec, installer_template, repo_template, require_gpg_check,
):
    bootstrap = (release_root / "install.bash").read_text(encoding="utf-8")
    release = re.escape(release_root.name)
    endpoint = re.findall(
        r'^run_agent_install_from_url[ 	]+"(https://[a-zA-Z0-9.:[\]_-]+(?:/[a-zA-Z0-9._-]+)+)/'
        + release + r'/\$\{INSTALLER_FILENAME\}[.]bash"',
        bootstrap, re.MULTILINE,
    )
    if len(endpoint) != 1:
        raise ValueError("Could not determine the package endpoint from PE's install.bash.")
    server = re.findall(
        r'^[ 	]*"\$\{PUPPET_BIN_DIR\}/puppet" config set server ([a-zA-Z0-9._-]+) --section main$',
        bootstrap, re.MULTILINE,
    )
    if len(server) != 1:
        raise ValueError("Could not determine the agent server setting from PE's install.bash.")
    packages_url = endpoint[0]
    values = {
        "BUILD_URL": "{}/{}/{}".format(packages_url, release_root.name, build),
        "PACKAGE_SPEC": package_spec,
        "PUPPET_SERVER": server[0],
        "PERCENT_OPEN": "%{",
        "GPG_CHECK": "1" if require_gpg_check else "0",
    }
    if require_gpg_check:
        values["KEY_URL"] = packages_url + "/GPG-KEY-puppet"
        values["KEY_CHECKSUM"] = digest(release_root.parent / "GPG-KEY-puppet")

    conditional = re.compile(r"^@@IF_GPG@@\n(.*?)^@@END_GPG@@\n", re.MULTILINE | re.DOTALL)
    token = re.compile(r"@@([A-Z_]+)@@")
    installer = conditional.sub(lambda match: match[1] if require_gpg_check else "", installer_template)
    repo = conditional.sub(lambda match: match[1] if require_gpg_check else "", repo_template)
    repo_tokens = {"BUILD_URL", "GPG_CHECK"}
    if require_gpg_check:
        repo_tokens.add("KEY_URL")
    if set(token.findall(repo)) != repo_tokens or set(token.findall(installer)) != set(values):
        raise ValueError("The EL7 templates are missing required tokens or contain unknown tokens.")

    return (
        token.sub(lambda match: values[match[1]], installer),
        token.sub(lambda match: values[match[1]], repo),
    )


def publish(
    el7_extended_support_agent_location, current_package_path, platform_tag,
    installer_template, repo_template, require_gpg_check=True,
):
    if os.geteuid() != 0:
        raise ValueError("The task must run as root on the PE primary.")
    facts = json.loads(command(
        "/opt/puppetlabs/bin/puppet", "facts", "show",
        "is_pe", "role", "pe_server_version", "--render-as", "json"
    ))
    if facts.get("is_pe") is not True or facts.get("role") != "peprimary":
        raise ValueError("The node must have is_pe=true and role=peprimary.")

    version = facts.get("pe_server_version")
    if not isinstance(version, str) or not re.fullmatch(r"2026[.][0-9]+(?:[.][0-9]+)*", version):
        raise ValueError("The node must run Puppet Enterprise 2026.")

    current = Path(current_package_path)
    package_root = current.parent.resolve(strict=True)
    release_root = current.resolve(strict=True)
    if not current.is_symlink() or not release_root.is_dir() or release_root.parent != package_root or not re.fullmatch(
        r"[0-9]+(?:[.][0-9]+)+", release_root.name
    ):
        raise ValueError("The current package link must point to a versioned PE release directory.")

    if release_root.name != version:
        raise ValueError("Current package directory {} does not match PE {}.".format(release_root, version))

    requested = Path(el7_extended_support_agent_location)
    if not requested.is_absolute() or requested.suffix != ".rpm":
        raise ValueError("el7_extended_support_agent_location must be an absolute RPM path on the PE primary.")
    source = requested.resolve(strict=True)
    if not source.is_file():
        raise ValueError("el7_extended_support_agent_location must point to a regular file.")

    if platform_tag != "el-7-x86_64":
        raise ValueError("The installer platform must be el-7-x86_64.")

    with tempfile.TemporaryDirectory(prefix=".publish-el7-", dir=release_root) as temporary:
        bundle = Path(temporary) / "repo"
        bundle.mkdir(mode=0o755)
        candidate = bundle / "candidate.rpm"
        shutil.copyfile(source, candidate)
        candidate.chmod(0o644)

        if require_gpg_check:
            signature = command("/usr/bin/rpm", "-Kv", str(candidate))
            if not re.search(r"Signature.*: OK$", signature, re.MULTILINE):
                raise ValueError("The RPM must have a signature trusted by the primary's RPM keyring.")
        query_format = "%{NAME} %{EPOCHNUM} %{VERSION} %{RELEASE} %{ARCH}"
        name, epoch, version, release, arch = command(
            "/usr/bin/rpm", "-qp", "--queryformat", query_format, str(candidate)
        ).split()
        if (name, arch) != ("puppet-agent", "x86_64") or not re.search(r"(?:^|[.])el7(?:[.]|$)", release):
            raise ValueError("Expected an EL7 x86_64 puppet-agent RPM.")
        if not re.fullmatch(r"[0-9]+", epoch) or any(
            not re.fullmatch(r"[A-Za-z0-9._-]+", value) for value in (version, release)
        ):
            raise ValueError("The package version contains unexpected characters.")

        build = "{}-{}-{}-{}".format(platform_tag, epoch, version, release)
        if not require_gpg_check:
            build += "-no-gpg"
        rpm_name = "{}-{}-{}.{}.rpm".format(name, version, release, arch)
        package_spec = rpm_name[:-4]
        if epoch != "0":
            package_spec = "{}-{}:{}-{}.{}".format(name, epoch, version, release, arch)
        destination = release_root / build
        installer_name = platform_tag + ".bash"
        repo_name = platform_tag + ".repo"
        installer, repo = render_installers(
            release_root, build, package_spec, installer_template, repo_template, require_gpg_check,
        )
        entry_points = {
            release_root / installer_name: platform_tag + "/" + installer_name,
            release_root / repo_name: platform_tag + "/" + repo_name,
        }
        active = release_root / platform_tag

        # Serialize publication so concurrent runs cannot overwrite a build.
        lock = os.open(str(release_root), os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.is_symlink():
                raise ValueError("A versioned repository cannot be a symlink: {}".format(destination))
            for entry, target in entry_points.items():
                if os.path.lexists(str(entry)) and not (
                    entry.is_symlink() and os.readlink(str(entry)) == target
                ):
                    raise ValueError("Refusing to replace an existing PE install file: {}".format(entry))
            if os.path.lexists(str(active)) and not (
                active.is_symlink() and re.fullmatch(
                    re.escape(platform_tag) + r"-[0-9]+-[A-Za-z0-9._-]+",
                    os.readlink(str(active)),
                )
            ):
                raise ValueError("Refusing to replace an existing PE platform entry: {}".format(active))

            changed = not destination.exists()
            if changed:
                createrepo = shutil.which("createrepo_c")
                if not createrepo:
                    raise ValueError("Install createrepo_c on the PE primary before publishing.")
                candidate.rename(bundle / rpm_name)
                command(createrepo, "--compress-type", "gz", str(bundle))
                if not (bundle / "repodata/repomd.xml").is_file():
                    raise ValueError("createrepo_c did not produce repodata/repomd.xml.")
                for filename, contents in ((installer_name, installer), (repo_name, repo)):
                    artifact = bundle / filename
                    artifact.write_text(contents, encoding="utf-8")
                    artifact.chmod(0o644)
                command("/bin/bash", "-n", str(bundle / installer_name))
            elif not (
                (destination / rpm_name).is_file()
                and digest(destination / rpm_name) == digest(candidate)
                and (destination / "repodata/repomd.xml").is_file()
                and (destination / installer_name).read_text(encoding="utf-8") == installer
                and (destination / repo_name).read_text(encoding="utf-8") == repo
            ):
                raise ValueError("Repository already exists with different contents: {}".format(destination))

            if current.resolve(strict=True) != release_root:
                raise ValueError("The current release changed during staging. Retry the plan.")
            if changed:
                bundle.rename(destination)
            for entry, target in entry_points.items():
                if not entry.is_symlink():
                    entry.symlink_to(target)
                    changed = True
            if not active.is_symlink() or os.readlink(str(active)) != build:
                staged_link = Path(temporary) / platform_tag
                staged_link.symlink_to(build)
                staged_link.replace(active)
                changed = True
        finally:
            os.close(lock)

    return {
        "changed": changed,
        "directory": str(destination),
        "pe_release": release_root.name,
        "rpm_path": str(destination / rpm_name),
        "installer_path": str(release_root / installer_name),
        "repo_path": str(release_root / repo_name),
    }


if __name__ == "__main__":
    try:
        parameters = json.load(sys.stdin)
        result = publish(
            el7_extended_support_agent_location=parameters["el7_extended_support_agent_location"],
            current_package_path=parameters["current_package_path"],
            platform_tag=parameters["platform_tag"],
            installer_template=parameters["installer_template"],
            repo_template=parameters["repo_template"],
            require_gpg_check=parameters.get("require_gpg_check") is not False,
        )
        print(json.dumps(result))
    except (KeyError, OSError, ValueError) as error:
        print(json.dumps({"_error": {"kind": "extended_support_agent/publish", "msg": str(error)}}))
        sys.exit(1)
