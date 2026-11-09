import hashlib
import tempfile
import unittest
from pathlib import Path

from translation_agent.runtime.digests import FileDigestCache, sha256_bytes, sha256_file
from unittest.mock import Mock


class DigestTests(unittest.TestCase):
    def test_file_digest_matches_bytes_digest_across_block_boundary(self) -> None:
        payload = b"x" * (1024 * 1024 + 7)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob.bin"
            path.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            self.assertEqual(sha256_file(path), expected)
            self.assertEqual(sha256_bytes(payload), expected)


    def test_scoped_cache_resolves_paths_and_clear_requires_a_new_full_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob.bin"
            path.write_bytes(b"original")
            cache = FileDigestCache()
            hashed = Mock(wraps=sha256_file)
            first = cache.sha256(path, hash_file=hashed)
            self.assertEqual(cache.sha256(path.parent / ".." / path.parent.name / path.name, hash_file=hashed), first)
            self.assertEqual(hashed.call_count, 1)
            cache.clear()
            self.assertEqual(cache.sha256(path, hash_file=hashed), first)
            self.assertEqual(hashed.call_count, 2)

    def test_scoped_cache_detects_same_size_replacement(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob.bin"
            path.write_bytes(b"original")
            cache = FileDigestCache()
            first = cache.sha256(path)
            stamp = path.stat()
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b"modified")
            os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            os.replace(replacement, path)
            self.assertNotEqual(cache.sha256(path), first)

    def test_a_write_during_hashing_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob.bin"
            path.write_bytes(b"original")
            cache = FileDigestCache()

            def writing_hasher(path):
                value = sha256_file(path)
                path.write_bytes(b"longer modified content")
                return value

            with self.assertRaisesRegex(ValueError, "file changed while hashing"):
                cache.sha256(path, hash_file=writing_hasher)
            self.assertEqual(cache.sha256(path), sha256_file(path))


if __name__ == "__main__":
    unittest.main()
