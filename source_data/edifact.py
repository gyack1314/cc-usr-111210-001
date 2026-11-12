"""UN/EDIFACT 语法版本 3 的字节扫描与段解码。

只把释放符后面的一个字节当作普通内容。扫描状态在字节块之间保留，
所以块大小不会改变段边界。所有错误都带原文件中的零基字节偏移。
"""
from dataclasses import dataclass
import hashlib

DEFAULT_COMPONENT = ':'
DEFAULT_ELEMENT = '+'
DEFAULT_DECIMAL = '.'
DEFAULT_RELEASE = '?'
DEFAULT_RESERVED = ' '
DEFAULT_TERMINATOR = "'"


@dataclass(frozen=True)
class Syntax:
    component: str
    element: str
    decimal: str
    release: str
    reserved: str
    terminator: str
    una_present: bool
    una_bytes: int

    @property
    def component_byte(self):
        return ord(self.component)

    @property
    def element_byte(self):
        return ord(self.element)

    @property
    def release_byte(self):
        return ord(self.release)

    @property
    def terminator_byte(self):
        return ord(self.terminator)

    def as_dict(self):
        return {
            'component': self.component,
            'element': self.element,
            'decimal': self.decimal,
            'release': self.release,
            'reserved': self.reserved,
            'terminator': self.terminator,
            'una_present': self.una_present,
            'una_bytes': self.una_bytes,
        }


DEFAULT_SYNTAX = Syntax(
    component=DEFAULT_COMPONENT,
    element=DEFAULT_ELEMENT,
    decimal=DEFAULT_DECIMAL,
    release=DEFAULT_RELEASE,
    reserved=DEFAULT_RESERVED,
    terminator=DEFAULT_TERMINATOR,
    una_present=False,
    una_bytes=0,
)


def _allowed_byte(value):
    """UNOC/ISO-8859-1 业务正文中允许的字节。

    TAB、LF、CR 被当作正文而不是换行格式；它们不会被自动去掉。
    C0 的其他控制字节、DEL 和 C1 控制字节不属于本口径的 EDI 正文。
    """
    if value in (0x09, 0x0A, 0x0D):
        return True
    if value < 0x20 or value == 0x7F or 0x80 <= value <= 0x9F:
        return False
    return True


def _service_byte(value):
    return _allowed_byte(value) and value != 0x20


class EdifactError(Exception):
    """带偏移和已知段的结构/扫描错误。"""

    def __init__(self, code, message, offset, segments=None, segment=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset
        self.segments = list(segments or [])
        self.segment = segment

    def as_dict(self):
        out = {
            'code': self.code,
            'message': self.message,
            'byte_offset': self.offset,
        }
        if self.segment is not None:
            out.update({
                'segment_ordinal': self.segment.ordinal,
                'segment_tag': self.segment.tag,
                'segment_raw': self.segment.raw,
            })
        return out


def detect_syntax(raw):
    """读取并核对 UNA；没有 UNA 时返回约定默认值。"""
    raw = bytes(raw)
    if not raw.startswith(b'UNA'):
        return DEFAULT_SYNTAX
    if len(raw) < 9:
        raise EdifactError('invalid_una', 'UNA 不足 9 字节', 0)
    values = raw[3:9]
    for index, value in enumerate(values):
        if not _allowed_byte(value):
            raise EdifactError('illegal_byte', 'UNA 服务字符含非法字节', 3 + index)
    # 位置顺序：组件、元素、十进制、释放、保留、段终止。
    if values[4] != 0x20:
        raise EdifactError('invalid_una', 'UNA 保留位必须为空格', 7)
    if len(set(values)) != 6:
        raise EdifactError('invalid_una', '服务字符必须互异', 3)
    for index in (0, 1, 2, 3, 5):
        if not _service_byte(values[index]):
            raise EdifactError('invalid_una', '服务字符必须是可打印字符', 3 + index)
    return Syntax(
        component=bytes((values[0],)).decode('latin-1'),
        element=bytes((values[1],)).decode('latin-1'),
        decimal=bytes((values[2],)).decode('latin-1'),
        release=bytes((values[3],)).decode('latin-1'),
        reserved=bytes((values[4],)).decode('latin-1'),
        terminator=bytes((values[5],)).decode('latin-1'),
        una_present=True,
        una_bytes=9,
    )


def split_fields(content, syntax):
    """把段正文切成标签和元素/组件，保留空组件和空元素。

    escaped_pairs 记录组件值内每个被释放的字面服务字符的零基段内偏移，
    以便调用方能回看原文而不用规范化后的值代替身份。
    """
    elements = []
    components = []
    current = bytearray()
    escaped = False
    escaped_pairs = []
    release = syntax.release_byte
    component = syntax.component_byte
    element = syntax.element_byte

    for index, value in enumerate(content):
        if escaped:
            current.append(value)
            escaped = False
        elif value == release:
            escaped_pairs.append(index)
            escaped = True
        elif value == component:
            components.append(bytes(current).decode('latin-1'))
            current = bytearray()
        elif value == element:
            components.append(bytes(current).decode('latin-1'))
            elements.append(components)
            components = []
            current = bytearray()
        else:
            current.append(value)

    if escaped:
        raise EdifactError('dangling_release', '段内释放符后没有字符', -1)
    components.append(bytes(current).decode('latin-1'))
    elements.append(components)

    tag_element = elements.pop(0)
    if len(tag_element) != 1 or not tag_element[0]:
        raise EdifactError('invalid_tag', '段标签为空或含组件分隔符', -1)
    return tag_element[0], elements, escaped_pairs


@dataclass
class Segment:
    ordinal: int
    byte_start: int
    byte_end: int
    tag: str
    elements: list
    text: str
    raw: str
    raw_sha256: str
    escaped_pairs: list

    def source_ref(self):
        return {
            'segment_ordinal': self.ordinal,
            'byte_start': self.byte_start,
            'byte_end': self.byte_end,
            'tag': self.tag,
            'raw': self.raw,
            'raw_sha256': self.raw_sha256,
        }


def _make_segment(raw, start, end, ordinal, syntax):
    content = raw[start:end]
    segment_raw = raw[start:end + 1].decode('latin-1')
    try:
        tag, elements, escaped_pairs = split_fields(content, syntax)
    except EdifactError as error:
        error.offset = start
        raise
    text = content.decode('latin-1')
    return Segment(
        ordinal=ordinal,
        byte_start=start,
        byte_end=end + 1,
        tag=tag,
        elements=elements,
        text=text,
        raw=segment_raw,
        raw_sha256=hashlib.sha256(raw[start:end + 1]).hexdigest(),
        escaped_pairs=escaped_pairs,
    )


def scan(raw, *, chunk_size=4096, max_segment_bytes=1048576, syntax=None):
    """按块读取原始字节并切段，返回 (syntax, segments)。

    chunk_size 只影响内部读取切片；释放符和未完成的段会在块边界之间
    继续，因此不同块大小得到同一组段和同一组原偏移。
    """
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError('chunk_size 必须为正整数')
    if type(max_segment_bytes) is not int or max_segment_bytes < 1:
        raise ValueError('max_segment_bytes 必须为正整数')
    raw = bytes(raw)
    if syntax is None:
        syntax = detect_syntax(raw)

    segments = []
    start = syntax.una_bytes
    escaped = False
    release_offset = None
    release = syntax.release_byte
    terminator = syntax.terminator_byte
    ordinal = 0

    for block_start in range(start, len(raw), chunk_size):
        block_end = min(block_start + chunk_size, len(raw))
        for offset in range(block_start, block_end):
            value = raw[offset]
            if not _allowed_byte(value):
                raise EdifactError(
                    'illegal_byte', '正文含非法字节', offset, segments
                )
            if escaped:
                escaped = False
                continue
            if value == release:
                escaped = True
                release_offset = offset
            elif value == terminator:
                if offset == start:
                    raise EdifactError(
                        'empty_segment', '出现空段', start, segments
                    )
                length = offset + 1 - start
                if length > max_segment_bytes:
                    raise EdifactError(
                        'segment_too_long', '段超过长度上限', start, segments
                    )
                segment = _make_segment(raw, start, offset, ordinal, syntax)
                segments.append(segment)
                ordinal += 1
                start = offset + 1

    if escaped:
        raise EdifactError(
            'dangling_release', '文件末尾的释放符后没有字符',
            release_offset, segments
        )
    if start < len(raw):
        raise EdifactError(
            'unclosed_segment', '段在文件末尾未闭合', start, segments
        )
    return syntax, segments
