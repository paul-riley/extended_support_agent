"""Test task behavior against the repository templates loaded by the RSpec runner."""

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch


module_root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publish_el7", module_root / "tasks/publish_el7.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
templates = json.load(sys.stdin)
RPM_HEADERS = bytes(96) + (bytes.fromhex("8eade801") + bytes(12)) * 2
RPM_XML = """<rpmHeader>
  <rpmTag name="Summary"><string>Test Puppet agent</string></rpmTag>
  <rpmTag name="Requirename"><string>/bin/sh</string><string>libexample</string></rpmTag>
  <rpmTag name="Requireflags"><integer>512</integer><integer>12</integer></rpmTag>
  <rpmTag name="Requireversion"><string/><string>2:1.4-3</string></rpmTag>
  <rpmTag name="Dirnames"><string>/etc/</string></rpmTag>
  <rpmTag name="Basenames"><string>puppetlabs</string></rpmTag>
  <rpmTag name="Dirindexes"><integer>0</integer></rpmTag>
  <rpmTag name="Filemodes"><integer>16877</integer></rpmTag>
  <rpmTag name="Fileflags"><integer>0</integer></rpmTag>
  <rpmTag name="Changelogname"><string>Author &amp; Contributor</string></rpmTag>
  <rpmTag name="Changelogtime"><integer>1700000000</integer></rpmTag>
  <rpmTag name="Changelogtext"><string>Package update</string></rpmTag>
</rpmHeader>"""


class PublishEl7Test(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        package_root = root / "public"
        self.release = package_root / "2026.0.2"
        self.release.mkdir(parents=True)
        self.current = package_root / "current"
        self.current.symlink_to(self.release)
        (self.release / "install.bash").write_text(
            '"${PUPPET_BIN_DIR}/puppet" config set server primary.example.test --section main\n'
            'run_agent_install_from_url "https://primary.example.test:8140/packages/2026.0.2/'
            '${INSTALLER_FILENAME}.bash" "$@"\n',
            encoding="utf-8",
        )
        self.key = package_root / "GPG-KEY-puppet"
        self.key.write_bytes(b"test signing key")
        self.rpm = root / "agent.rpm"
        self.rpm.write_bytes(RPM_HEADERS + b"test RPM")
        self.platform = templates["extended_support_agent::el7_platform_tag"]
        self.active = self.release / self.platform
        self.repo = self.release / (self.platform + ".repo")
        self.installer = self.release / (self.platform + ".bash")
        self.signature = "Header RSA Signature, key ID test: OK"
        self.rpm_release = "1.el7"
        self.pe_version = "2026.0.2"
        self.commands = []

        for patcher in (
            patch.object(publisher, "command", side_effect=self.run_command),
            patch.object(publisher.os, "geteuid", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *args):
        self.commands.append(args)
        if args[0].endswith("/puppet"):
            return json.dumps({"is_pe": True, "role": "peprimary", "pe_server_version": self.pe_version})
        if args[1] == "-Kv":
            return self.signature
        if args[1:3] == ("-qp", "--xml"):
            return RPM_XML
        if args[1] == "-qp":
            return "puppet-agent 0 8.22.2 {} x86_64".format(self.rpm_release)
        if args[:2] == ("/bin/bash", "-n"):
            subprocess.run(args, check=True)
            return ""
        self.fail("Unexpected command: {!r}".format(args))

    def publish(self, **options):
        return publisher.publish(
            el7_extended_support_agent_location=str(self.rpm),
            current_package_path=str(self.current),
            platform_tag=self.platform,
            installer_template=templates["extended_support_agent::el7_installer_template"],
            repo_template=templates["extended_support_agent::el7_repo_template"],
            **options,
        )

    def rpm_options(self):
        return [args[1] for args in self.commands if args[0] == "/usr/bin/rpm"]

    def test_default_checks_signature_and_publishes_signed_configuration(self):
        result = self.publish()

        self.assertEqual(self.rpm_options(), ["-Kv", "-qp", "-qp"])
        repo = self.repo.read_text()
        self.assertIn("gpgcheck=1", repo)
        self.assertIn("gpgkey=https://primary.example.test:8140/packages/GPG-KEY-puppet", repo)
        self.assertIn("puppet-agent-8.22.2-1.el7.x86_64", self.installer.read_text())
        self.assertEqual(self.active.resolve(), Path(result["directory"]))
        self.assertTrue((self.active / "repodata/repomd.xml").is_file())
        self.assertFalse(self.publish()["changed"])

    def test_metadata_contains_consistent_checksums_and_package_details(self):
        result = self.publish(require_gpg_check=False)
        repository = Path(result["directory"])
        manifest = ET.parse(repository / "repodata/repomd.xml")
        repo_ns = {"repo": "http://linux.duke.edu/metadata/repo"}
        documents = {}
        for entry in manifest.findall("repo:data", repo_ns):
            location = entry.find("repo:location", repo_ns).attrib["href"]
            compressed = (repository / location).read_bytes()
            xml = gzip.decompress(compressed)
            self.assertEqual(entry.findtext("repo:checksum", namespaces=repo_ns), hashlib.sha256(compressed).hexdigest())
            self.assertEqual(entry.findtext("repo:open-checksum", namespaces=repo_ns), hashlib.sha256(xml).hexdigest())
            documents[entry.attrib["type"]] = ET.fromstring(xml)

        self.assertEqual(set(documents), {"primary", "filelists", "other"})
        common = "{http://linux.duke.edu/metadata/common}"
        rpm = "{http://linux.duke.edu/metadata/rpm}"
        primary = documents["primary"].find(common + "package")
        checksum = hashlib.sha256(Path(result["rpm_path"]).read_bytes()).hexdigest()
        self.assertEqual(primary.findtext(common + "checksum"), checksum)
        dependencies = primary.find(common + "format/" + rpm + "requires")
        self.assertEqual(
            [entry.attrib for entry in dependencies],
            [{"name": "/bin/sh", "pre": "1"},
             {"name": "libexample", "flags": "GE", "epoch": "2", "ver": "1.4", "rel": "3"}],
        )
        file_entry = documents["filelists"].find(".//{http://linux.duke.edu/metadata/filelists}file")
        self.assertEqual((file_entry.text, file_entry.attrib["type"]), ("/etc/puppetlabs", "dir"))
        changelog = documents["other"].find(".//{http://linux.duke.edu/metadata/other}changelog")
        self.assertEqual((changelog.text, changelog.attrib["author"]), ("Package update", "Author & Contributor"))

    def test_default_rejects_unsigned_rpm(self):
        self.signature = "SHA256 digest: OK"

        with self.assertRaisesRegex(ValueError, "signature trusted"):
            self.publish()

        self.assertEqual(self.rpm_options(), ["-Kv"])
        self.assertFalse(self.active.is_symlink())

    def test_disabling_gpg_runs_only_rpm_metadata_query(self):
        self.key.unlink()

        self.publish(require_gpg_check=False)

        self.assertEqual(self.rpm_options(), ["-qp", "-qp"])
        self.assertIn("gpgcheck=0", self.repo.read_text())
        self.assertNotIn("gpgkey=", self.repo.read_text())
        self.assertNotIn("check_gpg_key", self.installer.read_text())

    def test_activating_another_build_preserves_the_first(self):
        first = self.publish()
        self.rpm_release = "2.el7"
        self.rpm.write_bytes(RPM_HEADERS + b"next RPM")

        second = self.publish()

        self.assertEqual(Path(first["rpm_path"]).read_bytes(), RPM_HEADERS + b"test RPM")
        self.assertEqual(self.active.resolve(), Path(second["directory"]))
        self.assertEqual(self.repo.resolve().parent, self.active.resolve())
        self.assertEqual(self.installer.resolve().parent, self.active.resolve())

    def test_conflicting_rpm_leaves_active_build_in_place(self):
        first = self.publish()
        self.rpm.write_bytes(RPM_HEADERS + b"different bytes, same package version")

        with self.assertRaisesRegex(ValueError, "already exists with different contents"):
            self.publish()

        self.assertEqual(self.active.resolve(), Path(first["directory"]))
        self.assertEqual(Path(first["rpm_path"]).read_bytes(), RPM_HEADERS + b"test RPM")

    def test_release_mismatch_stops_before_running_rpm(self):
        self.pe_version = "2026.0.3"

        with self.assertRaisesRegex(ValueError, "does not match PE"):
            self.publish()

        self.assertEqual(self.rpm_options(), [])
        self.assertFalse(self.active.is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
