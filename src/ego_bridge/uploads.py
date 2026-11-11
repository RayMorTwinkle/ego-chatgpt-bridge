"""Bounded client-provided research attachments."""
import base64
import binascii
import hashlib
import unicodedata

from .model import BridgeError


MAX_FILES = 4
MAX_BYTES = 40 * 1024 * 1024
MAX_PENDING_BYTES = 400 * 1024 * 1024
MAX_REQUEST_BYTES = 56 * 1024 * 1024


def decode_uploads(value):
    if value is None:
        return [], []
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise BridgeError('INVALID_ATTACHMENTS', '每轮最多可上传 4 个附件。')
    metadata, content, names = [], [], set()
    total = 0
    for file in value:
        if not isinstance(file, dict) or set(file) != {'filename', 'content_base64'}:
            raise BridgeError('INVALID_ATTACHMENTS', '每个附件需要 filename 和 content_base64。')
        name, encoded = file['filename'], file['content_base64']
        if (not isinstance(name, str) or not name or name in {'.', '..'} or name != name.strip()
                or len(name.encode('utf-8')) > 200 or any(c in '/\\:' or unicodedata.category(c).startswith('C') for c in name)):
            raise BridgeError('INVALID_ATTACHMENT_NAME', '附件必须使用单独的文件名，不能包含路径或控制字符。')
        canonical = unicodedata.normalize('NFC', name).casefold()
        if canonical in names:
            raise BridgeError('DUPLICATE_ATTACHMENT_NAME', '同一轮的附件文件名必须不同。')
        names.add(canonical)
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
            raise BridgeError('INVALID_ATTACHMENT_CONTENT', '附件需要有效的 Base64 内容，合计最多 40 MiB。')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BridgeError('INVALID_ATTACHMENT_CONTENT', '附件 Base64 内容无法解码。') from exc
        total += len(data)
        if not data or total > MAX_BYTES:
            raise BridgeError('ATTACHMENT_SIZE_LIMIT', '附件不可为空，合计最多 40 MiB。')
        metadata.append({'filename': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        content.append((name, data))
    return metadata, content
