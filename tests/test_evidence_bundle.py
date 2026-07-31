import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from evidence_bundle import BUNDLE_FORMAT, EvidenceBundleError, EvidenceBundleVerificationError, MANIFEST_NAME, pack_evidence_archive, list_evidence_files, read_evidence_bundle, validate_evidence_name, verify_evidence_bundle


class EvidenceBundleTests(unittest.TestCase):

    def setUp(self):
        self.temporary_folder = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary_folder.name)
        self.payloads = {
            'logs/fio-result.json': b'{"jobs": []}',
            'smart/extended-c0.bin': b'\x00\x01\x02',
            'telemetry/full.log': b'nvme telemetry',
        }

    def tearDown(self):
        self.temporary_folder.cleanup()

    def _bundle_path(self, name='evidence.zip'):
        return self.folder / name

    def _rewrite_bundle(self, source, destination, replacements=None, extras=None):
        replacements = replacements or {}
        extras = extras or {}
        with zipfile.ZipFile(source, 'r') as original:
            entries = [(info.filename, original.read(info)) for info in original.infolist()]
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_STORED) as target:
            for name, data in entries:
                target.writestr(name, replacements.get(name, data))
            for name, data in extras.items():
                target.writestr(name, data)

    def test_build_creates_sorted_bundle_with_sha256_manifest(self):
        result = pack_evidence_archive(self._bundle_path(), self.payloads, {'task_id': 'task-001', 'operator': 'qa'})
        self.assertEqual(result['manifest']['format'], BUNDLE_FORMAT)
        self.assertEqual(result['manifest']['metadata']['task_id'], 'task-001')
        self.assertEqual([item['name'] for item in result['files']], sorted(self.payloads))

        with zipfile.ZipFile(self._bundle_path(), 'r') as archive:
            self.assertEqual(archive.namelist(), [MANIFEST_NAME] + sorted(self.payloads))
            manifest = json.loads(archive.read(MANIFEST_NAME).decode('utf-8'))
        self.assertEqual(manifest['files'], result['files'])
        self.assertEqual(manifest['files'][0]['sha256'], '921e8f3293fce9361134d3b09cc4cf53f832e92dd9bc5c98bd865d2fadaf51e5')

    def test_identical_input_builds_byte_for_byte_deterministic_archives(self):
        first = self._bundle_path('first.zip')
        second = self._bundle_path('second.zip')
        pack_evidence_archive(first, list(reversed(list(self.payloads.items()))), {'task_id': 'task-001'})
        pack_evidence_archive(second, self.payloads, {'task_id': 'task-001'})
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_list_read_and_verify_return_original_payloads(self):
        pack_evidence_archive(self._bundle_path(), self.payloads, {'task_id': 'task-002'})
        listing = list_evidence_files(self._bundle_path())
        bundle = read_evidence_bundle(self._bundle_path())
        verification = verify_evidence_bundle(self._bundle_path())
        self.assertEqual([item['name'] for item in listing['files']], sorted(self.payloads))
        self.assertEqual(bundle['payloads'], self.payloads)
        self.assertTrue(verification['valid'])
        self.assertEqual(verification['errors'], [])
        self.assertEqual(verification['files'], listing['files'])

    def test_safe_names_reject_path_traversal_reserved_and_platform_paths(self):
        invalid_names = ['', '../result.json', '/result.json', 'logs/../result.json', 'logs//result.json', 'logs\\result.json', 'C:result.json', MANIFEST_NAME]
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(EvidenceBundleError):
                    validate_evidence_name(name)

    def test_build_rejects_duplicate_names_and_non_byte_payloads(self):
        with self.assertRaises(EvidenceBundleError):
            pack_evidence_archive(self._bundle_path(), [('one.log', b'a'), ('one.log', b'b')])
        with self.assertRaises(EvidenceBundleError):
            pack_evidence_archive(self._bundle_path(), {'one.log': 'not-bytes'})
        with self.assertRaises(EvidenceBundleError):
            pack_evidence_archive(self._bundle_path(), {})

    def test_metadata_must_be_json_object_with_standard_values(self):
        with self.assertRaises(EvidenceBundleError):
            pack_evidence_archive(self._bundle_path(), self.payloads, ['not', 'an', 'object'])
        with self.assertRaises(EvidenceBundleError):
            pack_evidence_archive(self._bundle_path(), self.payloads, {'not_json': set([1])})

    def test_read_detects_payload_checksum_tampering(self):
        original = self._bundle_path('original.zip')
        tampered = self._bundle_path('tampered.zip')
        pack_evidence_archive(original, self.payloads)
        self._rewrite_bundle(original, tampered, {'telemetry/full.log': b'NVME telemetry'})
        with self.assertRaises(EvidenceBundleVerificationError):
            read_evidence_bundle(tampered)
        result = verify_evidence_bundle(tampered)
        self.assertFalse(result['valid'])
        self.assertIn('SHA-256', result['errors'][0])

    def test_read_rejects_extra_zip_entry_and_non_deterministic_order(self):
        original = self._bundle_path('original.zip')
        extra = self._bundle_path('extra.zip')
        pack_evidence_archive(original, self.payloads)
        self._rewrite_bundle(original, extra, extras={'unexpected.txt': b'not listed'})
        with self.assertRaises(EvidenceBundleVerificationError):
            list_evidence_files(extra)

        reordered = self._bundle_path('reordered.zip')
        with zipfile.ZipFile(original, 'r') as archive:
            manifest = archive.read(MANIFEST_NAME)
            evidence = [(name, archive.read(name)) for name in sorted(self.payloads, reverse=True)]
        with zipfile.ZipFile(reordered, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_NAME, manifest)
            for name, content in evidence:
                archive.writestr(name, content)
        with self.assertRaises(EvidenceBundleVerificationError):
            read_evidence_bundle(reordered)

    def test_read_rejects_malformed_manifest_and_missing_bundle(self):
        malformed = self._bundle_path('malformed.zip')
        with zipfile.ZipFile(malformed, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_NAME, b'not json')
        with self.assertRaises(EvidenceBundleVerificationError):
            read_evidence_bundle(malformed)
        missing_result = verify_evidence_bundle(self._bundle_path('missing.zip'))
        self.assertFalse(missing_result['valid'])
        self.assertIn('不存在', missing_result['errors'][0])


if __name__ == '__main__':
    unittest.main()
