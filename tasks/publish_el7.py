#!/usr/bin/python3
"""Publish and activate a versioned EL7 installer on the PE primary."""

import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def command(*args):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise ValueError("{} failed: {}".format(args[0], (result.stderr or result.stdout).strip()))
    return result.stdout.strip()


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def rpm_header_range(path):
    # The RPM lead is followed by the signature header, padding, and the main header.
    def header_end(stream, start):
        stream.seek(start)
        magic, entries, store_size = struct.unpack(">4s4xII", stream.read(16))
        if magic != b"\x8e\xad\xe8\x01":
            raise ValueError("Invalid RPM header in {}".format(path.name))
        return start + 16 + entries * 16 + store_size

    with path.open("rb") as stream:
        start = (header_end(stream, 96) + 7) & ~7
        end = header_end(stream, start)
    if end > path.stat().st_size:
        raise ValueError("RPM header exceeds the file size: {}".format(path.name))
    return {"start": str(start), "end": str(end)}


def write_repository_metadata(path, name, epoch, version, release, arch):
    """Write the three rpm-md XML documents and their index for a single RPM."""
    header = ET.fromstring(command("/usr/bin/rpm", "-qp", "--xml", "--nodigest", "--nosignature", str(path)))
    tags = {tag.attrib["name"]: [item.text or "" for item in tag] for tag in header}

    def value(tag, fallback=""):
        return tags.get(tag, [fallback])[0]

    rpm_namespace = "http://linux.duke.edu/metadata/rpm"
    namespace = "http://linux.duke.edu/metadata/"
    primary = ET.Element("metadata", {"xmlns": namespace + "common", "xmlns:rpm": rpm_namespace, "packages": "1"})
    filelists = ET.Element("filelists", {"xmlns": namespace + "filelists", "packages": "1"})
    other = ET.Element("otherdata", {"xmlns": namespace + "other", "packages": "1"})
    package = ET.SubElement(primary, "package", {"type": "rpm"})
    package_id = digest(path)
    package_attrs = {"pkgid": package_id, "name": name, "arch": arch}
    file_package = ET.SubElement(filelists, "package", package_attrs)
    other_package = ET.SubElement(other, "package", package_attrs)

    ET.SubElement(package, "name").text = name
    ET.SubElement(package, "arch").text = arch
    version_attrs = {"epoch": epoch, "ver": version, "rel": release}
    for element in (package, file_package, other_package):
        ET.SubElement(element, "version", version_attrs)

    ET.SubElement(package, "checksum", {"type": "sha256", "pkgid": "YES"}).text = package_id
    for tag in ("Summary", "Description", "Packager", "Url"):
        ET.SubElement(package, tag.lower()).text = value(tag)
    timestamp = str(int(path.stat().st_mtime))
    ET.SubElement(package, "time", {"file": timestamp, "build": value("Buildtime", "0")})
    ET.SubElement(package, "size", {
        "package": str(path.stat().st_size),
        "installed": value("Longsize", value("Size", "0")),
        "archive": value("Longarchivesize", value("Archivesize", "0")),
    })
    ET.SubElement(package, "location", {"href": path.name})
    details = ET.SubElement(package, "format")
    for tag in ("License", "Vendor", "Group", "Buildhost", "Sourcerpm"):
        ET.SubElement(details, "rpm:" + tag.lower()).text = value(tag)
    ET.SubElement(details, "rpm:header-range", rpm_header_range(path))

    prereq_flags = 32 | 64 | 128 | 512 | 1024
    comparisons = {2: "LT", 4: "GT", 8: "EQ", 10: "LE", 12: "GE"}
    for family in ("Provide", "Require", "Conflict", "Obsolete", "Recommend", "Suggest", "Supplement", "Enhance"):
        names = tags.get(family + "name", [])
        if not names:
            continue
        entries = ET.SubElement(details, "rpm:" + family.lower() + "s")
        seen = set()
        for dependency, flags, evr in zip(names, tags[family + "flags"], tags[family + "version"]):
            if family == "Require" and dependency.startswith("rpmlib("):
                continue
            attributes = {"name": dependency}
            sense = int(flags)
            if evr and sense & 14 in comparisons:
                dep_epoch, separator, version_release = evr.partition(":")
                if not separator:
                    dep_epoch, version_release = "0", evr
                dep_version, separator, dep_release = version_release.rpartition("-")
                attributes.update({"flags": comparisons[sense & 14], "epoch": dep_epoch,
                                   "ver": dep_version if separator else version_release})
                if separator:
                    attributes["rel"] = dep_release
            # RPM marks prereqs and scriptlet requirements separately from comparisons.
            if family == "Require" and sense & prereq_flags:
                attributes["pre"] = "1"
            identity = tuple(attributes.items())
            if identity not in seen:
                ET.SubElement(entries, "rpm:entry", attributes)
                seen.add(identity)

    directories = tags.get("Dirnames", [])
    for base, directory, mode, flags in zip(*(tags.get(tag, []) for tag in (
        "Basenames", "Dirindexes", "Filemodes", "Fileflags",
    ))):
        filename = directories[int(directory)] + base
        attributes = {}
        if stat.S_ISDIR(int(mode)):
            attributes["type"] = "dir"
        elif int(flags) & 64:
            attributes["type"] = "ghost"
        ET.SubElement(file_package, "file", attributes).text = filename
        if filename.startswith("/etc/") or "/bin/" in filename or "/sbin/" in filename or filename == "/usr/lib/sendmail":
            ET.SubElement(details, "file", attributes).text = filename

    for author, date, text in zip(*(tags.get(tag, []) for tag in (
        "Changelogname", "Changelogtime", "Changelogtext",
    ))):
        ET.SubElement(other_package, "changelog", {"author": author, "date": date}).text = text

    repodata = path.parent / "repodata"
    repodata.mkdir()
    repomd = ET.Element("repomd", {"xmlns": namespace + "repo"})
    ET.SubElement(repomd, "revision").text = timestamp
    for kind, document in (("primary", primary), ("filelists", filelists), ("other", other)):
        xml_buffer = io.BytesIO()
        ET.ElementTree(document).write(xml_buffer, encoding="utf-8", xml_declaration=True)
        xml = xml_buffer.getvalue()
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as stream:
            stream.write(xml)
        compressed = buffer.getvalue()
        checksum = hashlib.sha256(compressed).hexdigest()
        filename = "{}-{}.xml.gz".format(checksum, kind)
        (repodata / filename).write_bytes(compressed)

        entry = ET.SubElement(repomd, "data", {"type": kind})
        ET.SubElement(entry, "checksum", {"type": "sha256"}).text = checksum
        ET.SubElement(entry, "open-checksum", {"type": "sha256"}).text = hashlib.sha256(xml).hexdigest()
        ET.SubElement(entry, "location", {"href": "repodata/" + filename})
        ET.SubElement(entry, "timestamp").text = timestamp
        ET.SubElement(entry, "size").text = str(len(compressed))
        ET.SubElement(entry, "open-size").text = str(len(xml))
    ET.ElementTree(repomd).write(str(repodata / "repomd.xml"), encoding="utf-8", xml_declaration=True)


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
            "/usr/bin/rpm", "-qp", "--queryformat", query_format, "--nodigest", "--nosignature", str(candidate)
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
                rpm_path = bundle / rpm_name
                candidate.rename(rpm_path)
                write_repository_metadata(rpm_path, name, epoch, version, release, arch)
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
    except (ET.ParseError, KeyError, OSError, ValueError, struct.error) as error:
        print(json.dumps({"_error": {"kind": "extended_support_agent/publish", "msg": str(error)}}))
        sys.exit(1)
