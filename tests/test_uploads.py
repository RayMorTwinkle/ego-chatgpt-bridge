import base64

import pytest

from ego_bridge.model import BridgeError
from ego_bridge.uploads import decode_uploads


def test_attachment_total_accepts_40_mib_and_rejects_an_extra_byte():
    encoded = base64.b64encode(b'x' * (20 * 1024 * 1024)).decode()
    files = [
        {'filename': 'one.bin', 'content_base64': encoded},
        {'filename': 'two.bin', 'content_base64': encoded},
    ]
    metadata, content = decode_uploads(files)
    assert sum(file['size_bytes'] for file in metadata) == 40 * 1024 * 1024
    del content
    files.append({'filename': 'extra.bin', 'content_base64': 'eA=='})
    with pytest.raises(BridgeError) as caught:
        decode_uploads(files)
    assert caught.value.data['error_code'] == 'ATTACHMENT_SIZE_LIMIT'
