import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path


BUNDLE_FORMAT = 'ssd-pressure-test-evidence-bundle'
MANIFEST_NAME = 'manifest.json'
MANIFEST_VERSION = 1
HASH_ALGORITHM = 'sha256'
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_EVIDENCE_FILES = 1000
MAX_EVIDENCE_FILE_BYTES = 512 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class EvidenceBundleError(ValueError):
    pass


class EvidenceBundleVerificationError(EvidenceBundleError):
    pass


def validate_evidence_name(name):
    if not isinstance(name, str):
        raise EvidenceBundleError('证据文件名必须是字符串')
    if not name or not name.strip():
        raise EvidenceBundleError('证据文件名不能为空')
    if '\x00' in name or '\\' in name:
        raise EvidenceBundleError('证据文件名不能包含空字符或反斜杠: {0}'.format(name))
    if name.startswith('/') or name.endswith('/'):
        raise EvidenceBundleError('证据文件名不能使用绝对路径或目录路径: {0}'.format(name))
    parts = name.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise EvidenceBundleError('证据文件名不能包含空目录或相对路径: {0}'.format(name))
    if any(':' in part for part in parts):
        raise EvidenceBundleError('证据文件名不能包含平台路径分隔符: {0}'.format(name))
    if name.casefold() == MANIFEST_NAME.casefold():
        raise EvidenceBundleError('证据文件名保留给清单: {0}'.format(name))
    return name


def _payload_bytes(value, name):
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise EvidenceBundleError('证据内容必须是字节数据: {0}'.format(name))


def _payload_items(payloads):
    if hasattr(payloads, 'items'):
        raw_items = list(payloads.items())
    else:
        try:
            raw_items = list(payloads)
        except TypeError:
            raise EvidenceBundleError('证据内容应为名称和字节数据的映射或二元组列表')

    items = []
    names = set()
    casefolded_names = set()
    for item in raw_items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise EvidenceBundleError('每条证据必须是名称和字节数据组成的二元组')
        name, content = item
        validate_evidence_name(name)
        if name in names or name.casefold() in casefolded_names:
            raise EvidenceBundleError('证据文件名重复或在大小写不敏感文件系统中冲突: {0}'.format(name))
        data = _payload_bytes(content, name)
        if len(data) > MAX_EVIDENCE_FILE_BYTES:
            raise EvidenceBundleError('单个证据文件超过大小限制: {0}'.format(name))
        names.add(name)
        casefolded_names.add(name.casefold())
        items.append((name, data))

    if not items:
        raise EvidenceBundleError('至少需要一份证据文件')
    if len(items) > MAX_EVIDENCE_FILES:
        raise EvidenceBundleError('证据文件数量超过限制')
    if sum(len(data) for _, data in items) > MAX_EVIDENCE_TOTAL_BYTES:
        raise EvidenceBundleError('证据总大小超过限制')
    return sorted(items, key=lambda item: item[0])


def _normalize_metadata(metadata):
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise EvidenceBundleError('证据清单元数据必须是对象')
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        normalized = json.loads(encoded)
    except (TypeError, ValueError):
        raise EvidenceBundleError('证据清单元数据必须可序列化为标准 JSON')
    return normalized


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _build_manifest(items, metadata):
    files = []
    for name, data in items:
        files.append({
            'name': name,
            'sha256': _sha256(data),
            'size_bytes': len(data),
        })
    return {
        'format': BUNDLE_FORMAT,
        'hash_algorithm': HASH_ALGORITHM,
        'metadata': metadata,
        'files': files,
        'version': MANIFEST_VERSION,
    }


def _zip_info(name):
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_evidence_bundle(bundle_path, payloads, metadata=None):
    target = Path(bundle_path)
    items = _payload_items(payloads)
    manifest = _build_manifest(items, _normalize_metadata(metadata))
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix='{0}.'.format(target.name), suffix='.tmp', dir=str(target.parent))
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.comment = b''
            archive.writestr(_zip_info(MANIFEST_NAME), _canonical_json(manifest))
            for name, data in items:
                archive.writestr(_zip_info(name), data)
        os.replace(temporary_path, str(target))
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise
    return {
        'path': str(target),
        'manifest': manifest,
        'files': list(manifest['files']),
    }


def _open_archive(bundle_path):
    target = Path(bundle_path)
    if not target.exists():
        raise EvidenceBundleVerificationError('证据包不存在: {0}'.format(target))
    if not target.is_file():
        raise EvidenceBundleVerificationError('证据包路径不是普通文件: {0}'.format(target))
    try:
        archive = zipfile.ZipFile(str(target), 'r')
    except (OSError, zipfile.BadZipFile) as exc:
        raise EvidenceBundleVerificationError('证据包不是有效 ZIP 文件: {0}'.format(exc))
    return archive


def _validate_initial_archive_layout(archive):
    infos = archive.infolist()
    if not infos:
        raise EvidenceBundleVerificationError('证据包为空')
    if len(infos) > MAX_EVIDENCE_FILES + 1:
        raise EvidenceBundleVerificationError('证据包中的文件数量超过限制')
    if archive.comment:
        raise EvidenceBundleVerificationError('证据包不允许包含额外 ZIP 注释')
    names = []
    for info in infos:
        name = info.filename
        if name.endswith('/'):
            raise EvidenceBundleVerificationError('证据包不允许包含目录项: {0}'.format(name))
        if name == MANIFEST_NAME:
            names.append(name)
            continue
        try:
            validate_evidence_name(name)
        except EvidenceBundleError as exc:
            raise EvidenceBundleVerificationError(str(exc))
        names.append(name)
    if len(names) != len(set(names)):
        raise EvidenceBundleVerificationError('证据包中存在重复文件名')
    if infos[0].filename != MANIFEST_NAME:
        raise EvidenceBundleVerificationError('证据清单必须是 ZIP 包中的第一个文件')
    if names.count(MANIFEST_NAME) != 1:
        raise EvidenceBundleVerificationError('证据包必须且只能包含一个清单文件')
    return infos


def _load_manifest(archive, manifest_info):
    if manifest_info.file_size > MAX_EVIDENCE_FILE_BYTES:
        raise EvidenceBundleVerificationError('证据清单超过大小限制')
    try:
        raw_manifest = archive.read(manifest_info)
        manifest = json.loads(raw_manifest.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, zipfile.BadZipFile) as exc:
        raise EvidenceBundleVerificationError('证据清单不是有效 UTF-8 JSON: {0}'.format(exc))
    if not isinstance(manifest, dict):
        raise EvidenceBundleVerificationError('证据清单根节点必须是对象')
    return manifest


def _validate_manifest(manifest):
    if manifest.get('format') != BUNDLE_FORMAT:
        raise EvidenceBundleVerificationError('证据清单格式标识不匹配')
    if manifest.get('version') != MANIFEST_VERSION:
        raise EvidenceBundleVerificationError('证据清单版本不受支持')
    if manifest.get('hash_algorithm') != HASH_ALGORITHM:
        raise EvidenceBundleVerificationError('证据清单哈希算法不受支持')
    if not isinstance(manifest.get('metadata'), dict):
        raise EvidenceBundleVerificationError('证据清单元数据必须是对象')
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        raise EvidenceBundleVerificationError('证据清单必须包含至少一份证据文件')
    if len(files) > MAX_EVIDENCE_FILES:
        raise EvidenceBundleVerificationError('证据清单中的文件数量超过限制')

    total_size = 0
    names = []
    casefolded_names = set()
    descriptors = []
    for descriptor in files:
        if not isinstance(descriptor, dict):
            raise EvidenceBundleVerificationError('证据清单文件项必须是对象')
        name = descriptor.get('name')
        digest = descriptor.get('sha256')
        size = descriptor.get('size_bytes')
        try:
            validate_evidence_name(name)
        except EvidenceBundleError as exc:
            raise EvidenceBundleVerificationError(str(exc))
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
            raise EvidenceBundleVerificationError('证据清单中的 SHA-256 值格式错误: {0}'.format(name))
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_EVIDENCE_FILE_BYTES:
            raise EvidenceBundleVerificationError('证据清单中的文件大小错误: {0}'.format(name))
        if name.casefold() in casefolded_names:
            raise EvidenceBundleVerificationError('证据清单中存在重复或大小写冲突的文件名: {0}'.format(name))
        total_size += size
        names.append(name)
        casefolded_names.add(name.casefold())
        descriptors.append({'name': name, 'sha256': digest, 'size_bytes': size})
    if total_size > MAX_EVIDENCE_TOTAL_BYTES:
        raise EvidenceBundleVerificationError('证据清单声明的总文件大小超过限制')
    if names != sorted(names):
        raise EvidenceBundleVerificationError('证据清单文件必须按名称排序')
    return descriptors


def _validate_archive_matches_manifest(archive, infos, descriptors):
    expected_names = [MANIFEST_NAME] + [descriptor['name'] for descriptor in descriptors]
    actual_names = [info.filename for info in infos]
    if actual_names != expected_names:
        raise EvidenceBundleVerificationError('ZIP 文件清单与证据清单不一致')
    if any(info.compress_type != zipfile.ZIP_STORED for info in infos):
        raise EvidenceBundleVerificationError('证据包必须使用无压缩 ZIP 条目保存')
    for info, descriptor in zip(infos[1:], descriptors):
        if info.file_size != descriptor['size_bytes']:
            raise EvidenceBundleVerificationError('证据文件大小与清单不一致: {0}'.format(descriptor['name']))


def _inspect_evidence_bundle(bundle_path):
    archive = _open_archive(bundle_path)
    try:
        infos = _validate_initial_archive_layout(archive)
        manifest = _load_manifest(archive, infos[0])
        descriptors = _validate_manifest(manifest)
        _validate_archive_matches_manifest(archive, infos, descriptors)
        return archive, manifest, descriptors
    except Exception:
        archive.close()
        raise


def list_evidence_files(bundle_path):
    archive, manifest, descriptors = _inspect_evidence_bundle(bundle_path)
    try:
        return {
            'manifest': manifest,
            'files': [dict(descriptor) for descriptor in descriptors],
        }
    finally:
        archive.close()


def read_evidence_bundle(bundle_path):
    archive, manifest, descriptors = _inspect_evidence_bundle(bundle_path)
    try:
        payloads = {}
        files = []
        for descriptor in descriptors:
            name = descriptor['name']
            try:
                data = archive.read(name)
            except (OSError, zipfile.BadZipFile) as exc:
                raise EvidenceBundleVerificationError('无法读取证据文件 {0}: {1}'.format(name, exc))
            if len(data) != descriptor['size_bytes']:
                raise EvidenceBundleVerificationError('证据文件读取长度不正确: {0}'.format(name))
            actual_digest = _sha256(data)
            if actual_digest != descriptor['sha256']:
                raise EvidenceBundleVerificationError('证据文件 SHA-256 校验失败: {0}'.format(name))
            payloads[name] = data
            files.append(dict(descriptor))
        return {
            'manifest': manifest,
            'files': files,
            'payloads': payloads,
        }
    finally:
        archive.close()


def verify_evidence_bundle(bundle_path):
    try:
        bundle = read_evidence_bundle(bundle_path)
    except (EvidenceBundleError, OSError) as exc:
        return {
            'valid': False,
            'manifest': None,
            'files': [],
            'errors': [str(exc)],
        }
    return {
        'valid': True,
        'manifest': bundle['manifest'],
        'files': bundle['files'],
        'errors': [],
    }
