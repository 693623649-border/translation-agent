import hashlib
import tempfile
import unittest
from pathlib import Path

from translation_agent.runtime.digests import sha256_bytes, sha256_file


class DigestTests(unittest.TestCase):
    def test_file_digest_matches_bytes_digest_across_block_boundary(self) -> None:
        payload = b"x" * (1024 * 1024 + 7)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob.bin"
            path.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(sha256_file(path), expected)
            self.assertEqual(sha256_bytes(payload), expected)


if __name__ == "__main__":
    unittest.main()
