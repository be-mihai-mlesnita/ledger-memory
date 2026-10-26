"""Synthetic, in-memory checks for the publication audit; no real data is used."""
import io
import json
import re
from pathlib import Path
import sys
import tarfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_repository import Audit


def zipped(name, content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, content)
    return output.getvalue()


class AuditTests(unittest.TestCase):
    def test_plain_source_passes(self):
        audit = Audit()
        audit.scan("example.py", b"def add(a, b): return a + b\n")
        self.assertEqual(audit.findings, [])

    def test_secret_inside_nested_archive_is_redacted(self):
        secret = "Bearer " + "Q" * 32
        audit = Audit()
        audit.scan("outer.zip", zipped("inner.zip", zipped("example.txt", secret)))
        self.assertTrue(any(row["code"] == "bearer_literal" for row in audit.findings))
        self.assertNotIn(secret, json.dumps(audit.findings))
        self.assertTrue(any("outer.zip!inner.zip!example.txt" == row["path"] for row in audit.findings))

    def test_runtime_file_is_blocked_even_without_secret_content(self):
        audit = Audit()
        audit.scan("core.zip", zipped("nested/auth.key", "not a usable credential"))
        self.assertTrue(any(row["code"] == "private_or_runtime_file" for row in audit.findings))

    def test_archive_path_traversal_is_blocked(self):
        audit = Audit()
        audit.scan("example.zip", zipped("../elsewhere.py", "pass"))
        self.assertTrue(any(row["code"] == "unsafe_path" for row in audit.findings))

    def test_tar_secret_detection(self):
        secret = ("ghp_" + "Q" * 36).encode()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            item = tarfile.TarInfo("example.txt")
            item.size = len(secret)
            archive.addfile(item, io.BytesIO(secret))
        audit = Audit()
        audit.scan("evidence.tar.gz", output.getvalue())
        self.assertTrue(any(row["code"] == "github_token" for row in audit.findings))
        self.assertNotIn(secret.decode(), json.dumps(audit.findings))

    def test_unknown_binary_requires_review(self):
        audit = Audit()
        audit.scan("unexpected.bin", b"\x00\x01\xff")
        self.assertTrue(any(row["code"] == "unreviewed_binary" for row in audit.findings))

    def test_reviewed_literal_exemption_is_exact(self):
        import hashlib
        one, two = "Bearer " + "Q" * 32, "Bearer " + "R" * 32
        audit = Audit()
        audit.allow[hashlib.sha256(one.encode()).hexdigest()] = "Synthetic test fixture"
        audit.scan("first.txt", one.encode())
        audit.scan("second.txt", two.encode())
        self.assertEqual(audit.allowed_matches, 1)
        self.assertTrue(audit.findings)

    def test_local_content_policy_checks_nested_archives(self):
        audit = Audit()
        audit.policy_text = [("restricted", re.compile("private-sentinel"))]
        audit.scan("package.zip", zipped("nested.zip", zipped("notes.txt", "private-sentinel")))
        self.assertTrue(any(row["code"] == "publication_policy_content" for row in audit.findings))

    def test_local_path_policy_blocks_embedded_files(self):
        audit = Audit()
        audit.policy_paths = [("restricted", re.compile(r"(?:^|!)blocked\.txt$"))]
        audit.scan("package.zip", zipped("blocked.txt", "ordinary content"))
        self.assertTrue(any(row["code"] == "publication_policy_path" for row in audit.findings))


if __name__ == "__main__":
    unittest.main()
