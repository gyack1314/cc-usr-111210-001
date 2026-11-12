"""UNB/UNZ 与 UNH/UNT 的逐层包络核对。

所有检查和结构解析只在内存中完成，不产生可见收货记录。
"""
from dataclasses import dataclass, field

from .edifact import EdifactError


def element(elements, index):
    """取第 index 个元素（组件列表）；缺失返回 None。"""
    if index >= len(elements):
        return None
    return elements[index]


def component(elements, element_index, component_index=0, default=None):
    """取某元素的某组件字符串；保留空组件，缺失才返回 default。"""
    item = element(elements, element_index)
    if item is None or component_index >= len(item):
        return default
    value = item[component_index]
    return default if value is None else value


def require_component(elements, element_index, name, component_index=0):
    value = component(elements, element_index, component_index)
    if value is None or value == '':
        raise ValueError('%s 缺失' % name)
    return value


def non_negative_integer_text(text, name):
    """核对非负整数词法串（PAC 包数），返回原词法串。"""
    if not isinstance(text, str) or text == '':
        raise ValueError('%s 缺失' % name)
    body = text[1:] if text[:1] == '+' else text
    if not body.isascii() or not body.isdigit():
        raise ValueError('%s 必须为非负整数' % name)
    return text


@dataclass
class Envelope:
    """交换层和消息层原位置，以及已验证的计数。"""
    unb: object
    unz: object
    messages: list = field(default_factory=list)

    def source_ref(self):
        return {
            'unb': self.unb.source_ref(),
            'unz': self.unz.source_ref(),
        }


def check_exchange(syntax, segments):
    """核对 UNB/UNZ 并把段划分成消息，返回 Envelope。

    任何一项不符都抛出 EdifactError；调用方据此隔离整个交换。
    """
    if not segments or segments[0].tag != 'UNB':
        offset = segments[0].byte_start if segments else syntax.una_bytes
        raise EdifactError('missing_unb', '交换必须以 UNB 开始', offset, segments)
    unb = segments[0]
    if len(unb.elements) < 5:
        raise EdifactError('invalid_unb', 'UNB 元素不足', unb.byte_start, segments, unb)
    syntax_id = unb.elements[0]
    if (
        len(syntax_id) < 2
        or syntax_id[0] != 'UNOC'
        or syntax_id[1] != '3'
    ):
        raise EdifactError(
            'unsupported_syntax_version',
            '只支持语法版本 3',
            unb.byte_start, segments, unb,
        )
    try:
        unb_control = require_component(unb.elements, 4, 'UNB 控制引用')
    except (ValueError, IndexError) as error:
        raise EdifactError('invalid_unb', str(error), unb.byte_start, segments, unb)

    if len(segments) < 3 or segments[-1].tag != 'UNZ':
        offset = segments[-1].byte_start if segments else syntax.una_bytes
        raise EdifactError('missing_unz', '交换必须以 UNZ 结束', offset, segments)
    unz = segments[-1]

    message_count = 0
    index = 1
    messages = []
    while index < len(segments) - 1:
        if segments[index].tag != 'UNH':
            raise EdifactError(
                'unexpected_segment', '消息层只能以 UNH 开始',
                segments[index].byte_start, segments, segments[index],
            )
        unh = segments[index]
        index += 1
        start = index
        while index < len(segments) - 1 and segments[index].tag not in ('UNH', 'UNT', 'UNZ'):
            index += 1
        if index < len(segments) - 1 and segments[index].tag in ('UNH', 'UNZ'):
            raise EdifactError(
                'unexpected_segment', '消息内出现嵌套的 UNH 或 UNZ',
                segments[index].byte_start, segments, segments[index],
            )
        unt = segments[index]
        if unt.tag != 'UNT':
            raise EdifactError('missing_unt', '消息没有 UNT', unt.byte_start, segments, unt)
        index += 1
        messages.append((unh, segments[start:index - 1], unt))
        message_count += 1

    if message_count == 0:
        raise EdifactError('missing_unh', '交换中没有消息', unb.byte_end, segments)

    for unh, body, unt in messages:
        try:
            ref_in = require_component(unh.elements, 0, 'UNH 消息引用')
            ref_out = require_component(unt.elements, 1, 'UNT 消息引用')
        except (IndexError, ValueError) as error:
            raise EdifactError('invalid_unt', str(error), unt.byte_start, segments, unt)
        if ref_in != ref_out:
            raise EdifactError(
                'message_reference_mismatch', 'UNH 与 UNT 消息引用不一致',
                unt.byte_start, segments, unt,
            )
        if len(unt.elements) < 2:
            raise EdifactError('invalid_unt', 'UNT 缺少段数', unt.byte_start, segments, unt)
        try:
            declared = int(non_negative_integer_text(
                require_component(unt.elements, 0, 'UNT 段数'), 'UNT 段数'
            ))
        except ValueError:
            raise EdifactError('invalid_unt', 'UNT 段数不是整数', unt.byte_start, segments, unt)
        actual = len(body) + 2
        if declared != actual:
            raise EdifactError(
                'segment_count_mismatch',
                'UNT 段数与消息实际段数不一致',
                unt.byte_start, segments, unt,
            )


    try:
        declared_messages = int(non_negative_integer_text(
            require_component(unz.elements, 0, 'UNZ 消息数'), 'UNZ 消息数'
        ))
        declared_control = require_component(unz.elements, 1, 'UNZ 控制引用')
    except IndexError:
        raise EdifactError('invalid_unz', 'UNZ 元素不足', unz.byte_start, segments, unz)
    except ValueError as error:
        raise EdifactError('invalid_unz', str(error), unz.byte_start, segments, unz)
    if declared_messages != message_count:
        raise EdifactError(
            'message_count_mismatch', 'UNZ 消息数与实际消息数不一致',
            unz.byte_start, segments, unz,
        )
    if declared_control != unb_control:
        raise EdifactError(
            'control_reference_mismatch', 'UNZ 与 UNB 控制引用不一致',
            unz.byte_start, segments, unz,
        )

    return Envelope(unb=unb, unz=unz, messages=messages)
