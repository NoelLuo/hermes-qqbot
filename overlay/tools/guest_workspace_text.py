"""Strict, read-only decoding for bounded guest text operations.

GB18030 is a compatibility fallback, not proof of the file's original encoding.
No replacement characters, content rewriting or arbitrary codec arguments.
"""
import codecs


class TextDecodingError(UnicodeError):
    """A safe, path-free error distinguishable from an authorization failure."""


_BOMS = (
    (codecs.BOM_UTF32_LE, 'utf-32', 'utf-32-le'),
    (codecs.BOM_UTF32_BE, 'utf-32', 'utf-32-be'),
    (codecs.BOM_UTF8, 'utf-8-sig', 'utf-8-sig'),
    (codecs.BOM_UTF16_LE, 'utf-16', 'utf-16-le'),
    (codecs.BOM_UTF16_BE, 'utf-16', 'utf-16-be'),
)


def decode_text(data: bytes) -> dict:
    """Decode already size-bounded bytes without changing them or guessing binary."""
    selected = next(((codec, name) for bom, codec, name in _BOMS if data.startswith(bom)), None)
    if selected:
        codec, encoding = selected
        try:
            text = data.decode(codec)
        except UnicodeError:
            raise TextDecodingError('Text decoding failed: malformed BOM-marked Unicode text.') from None
        detection = 'bom'
    else:
        try:
            text = data.decode('utf-8')
            encoding, detection = 'utf-8', 'utf8'
        except UnicodeError:
            try:
                text = data.decode('gb18030')
                encoding, detection = 'gb18030', 'fallback'
            except UnicodeError:
                raise TextDecodingError(
                    'Text decoding failed: unsupported encoding or binary data; '
                    'supported: UTF-8, BOM-marked UTF-16/UTF-32, GB18030.') from None
    if any((ord(char) < 32 and char not in '\t\n\r\f') or 127 <= ord(char) <= 159 for char in text):
        raise TextDecodingError('Text decoding failed: binary/control data or BOM-less Unicode is not supported.')
    return {'text': text, 'encoding': encoding, 'encoding_detection': detection}
