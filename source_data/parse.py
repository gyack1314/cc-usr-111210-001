"""DESADV 约定子集的层级解析（CPS / PAC / LIN / PIA / IMD / QTY）。

只依据段本身和当前消息内的结构，不猜测未知业务段的含义。
解析先收集全部 CPS 再解析父子关系，因此父节点可以稍后出现。
"""
from dataclasses import dataclass
from decimal import Decimal

from .edifact import EdifactError
from .envelope import component, non_negative_integer_text


EXCHANGE_TAGS = ('UNB', 'UNH', 'UNT', 'UNZ')


class StructureError(EdifactError):
    """消息结构错误；统一带消息内段序号和原段引用。"""


def _fail(code, message, segment, segments):
    return StructureError(code, message, segment.byte_start, segments, segment)


def decimal_value(text, syntax):
    """按 UNA 的十进制符号核对词法并构造 Decimal。

    只接受可选加号、十进制数字、至多一个十进制符号，不接受指数、
    负号和空白；不经过 float，也不改动词法。
    """
    body = text
    if body[:1] == '+':
        body = body[1:]
    elif body[:1] == '-':
        raise ValueError('数量不能为负')
    separator = syntax.decimal
    if body == '' or body.count(separator) > 1:
        raise ValueError('数量不是合法十进制数')
    whole, _, fraction = body.partition(separator)
    digits = whole + fraction
    if digits == '' or not digits.isascii() or not digits.isdigit():
        raise ValueError('数量含非数字字符')
    normalized = body.replace(separator, '.')
    value = Decimal(normalized)
    if not value.is_finite() or value < 0:
        raise ValueError('数量不能为负或非有限')
    return value


@dataclass
class Pack:
    segment: object
    parent_cps: object
    count_text: object = None
    count: object = None
    type: str = None
    declared_lins: list = None

    def as_dict(self):
        return {
            'count_text': self.count_text,
            'count': self.count,
            'type': self.type,
            'declared_lins': self.declared_lins,
            'cps': self.parent_cps.segment.source_ref(),
            'source': self.segment.source_ref(),
        }


@dataclass
class Line:
    segment: object
    cps: object
    item_number: str
    code_system: str
    additional: list
    descriptions: list
    quantities: list

    def as_dict(self):
        return {
            'item_number': self.item_number,
            'code_system': self.code_system,
            'additional': self.additional,
            'descriptions': self.descriptions,
            'quantities': self.quantities,
            'cps': self.cps.segment.source_ref(),
            'source': self.segment.source_ref(),
        }


@dataclass
class Cps:
    segment: object
    parent_id: str = None
    parent: object = None
    children: list = None
    packs: list = None
    lines: list = None

    def as_dict(self):
        return {
            'id': (
                self.segment.elements[0][0]
                if self.segment.elements and self.segment.elements[0] else None
            ),
            'parent_id': self.parent_id,
            'parent': (
                self.parent.segment.source_ref()
                if self.parent is not None else None
            ),
            'children': [child.as_dict() for child in self.children],
            'packs': [pack.as_dict() for pack in self.packs],
            'lines': [line.as_dict() for line in self.lines],
            'source': self.segment.source_ref(),
        }


@dataclass
class Message:
    unh: object
    unt: object
    control_reference: str
    message_reference: str
    syntax_version: str
    bgm: object = None
    nodes: dict = None
    roots: list = None
    unknown: list = None

    def as_dict(self):
        return {
            'message_reference': self.message_reference,
            'control_reference': self.control_reference,
            'syntax_version': self.syntax_version,
            'bgm': self.bgm.source_ref() if self.bgm is not None else None,
            'roots': [node.as_dict() for node in self.roots],
            'unknown': [
                {
                    'segment': segment.source_ref(),
                    'elements': segment.elements,
                }
                for segment in self.unknown
            ],
            'source': {'unh': self.unh.source_ref(), 'unt': self.unt.source_ref()},
        }


def _parse_quantity(segment, segments, cps, line, syntax):
    """QTY 只在当前 LIN 下有效；限定符为 12 时标记为发运数量。"""
    # QTY 第 1 元素是限定符:数量的复合值。
    qualifier = component(segment.elements, 0, 0, '')
    raw = component(segment.elements, 0, 1)
    if qualifier == '':
        raise _fail('invalid_qty', 'QTY 缺少限定符', segment, segments)
    if raw is None or raw == '':
        raise _fail('invalid_qty', 'QTY 缺少数量', segment, segments)
    try:
        value = decimal_value(raw, syntax)
    except ValueError as error:
        raise _fail('invalid_qty', '数量不合法：%s' % error, segment, segments)
    return {
        'qualifier': qualifier,
        'raw': raw,
        'value': value,
        'shipped': qualifier == '12',
        'cps': cps.segment.source_ref(),
        'line': line.segment.source_ref(),
        'source': segment.source_ref(),
    }


def parse_message(unh, unt, body, segments, syntax):
    """解析一条 DESADV 消息；任何结构错误都抛出 StructureError。"""
    try:
        message_ref = unh.elements[0][0]
        identifier = unh.elements[1]
    except IndexError:
        raise _fail('invalid_unh', 'UNH 元素不足', unh, segments)
    if len(identifier) < 3 or identifier[0] != 'DESADV':
        raise _fail('unsupported_message', '只处理 DESADV 消息', unh, segments)
    # 语法版本由 UNB 的 UNOC:3 决定；这里的 D 是 DESADV 的目录版本。
    version = '3'

    nodes = {}
    bgm = None
    unknown = []
    current_cps = None
    current_line = None
    bgm_position = None

    for segment in body:
        tag = segment.tag
        if bgm is not None and tag == 'BGM':
            raise _fail('duplicate_bgm', '消息中出现多个 BGM', segment, segments)
        if tag == 'BGM':
            if body[0] is not segment:
                raise _fail('unexpected_segment', 'BGM 必须是 UNH 后的首段', segment, segments)
            bgm = segment
        elif tag == 'CPS':
            if segment.ordinal < 1 or not segment.elements or component(segment.elements, 0, 0) == '':
                raise _fail('invalid_cps', 'CPS 缺少包裹 ID', segment, segments)
            cps_id = component(segment.elements, 0, 0)
            if cps_id in nodes:
                raise _fail('duplicate_cps', 'CPS 包裹 ID 重复', segment, segments)
            parent_id = component(segment.elements, 1, 0)
            node = Cps(
                segment=segment,
                parent_id=(parent_id if parent_id not in (None, '') else None),
                children=[],
                packs=[],
                lines=[],
            )
            nodes[cps_id] = node
            current_cps = node
            current_line = None
        elif tag == 'PAC':
            if current_cps is None:
                raise _fail('unexpected_segment', 'PAC 位于 CPS 之前', segment, segments)
            count_text = component(segment.elements, 0, 0)
            pack_type = component(segment.elements, 2, 0)
            declared = component(segment.elements, 3, 0)
            count = None
            if count_text not in (None, ''):
                try:
                    non_negative_integer_text(count_text, 'PAC 包数')
                    count = int(count_text)
                except ValueError as error:
                    raise _fail('invalid_pac', str(error), segment, segments)
            if declared not in (None, ''):
                try:
                    declared = non_negative_integer_text(declared, 'PAC 声明行数')
                except ValueError as error:
                    raise _fail('invalid_pac', str(error), segment, segments)
            else:
                declared = None
            current_cps.packs.append(Pack(
                segment=segment,
                parent_cps=current_cps,
                count_text=count_text,
                count=count,
                type=pack_type,
                declared_lins=declared,
            ))
        elif tag == 'LIN':
            if current_cps is None:
                raise _fail('unexpected_segment', 'LIN 位于 CPS 之前', segment, segments)
            item_number = component(segment.elements, 2, 0, '') or ''
            code_system = component(segment.elements, 2, 1, '') or ''
            line = Line(
                segment=segment,
                cps=current_cps,
                item_number=item_number,
                code_system=code_system,
                additional=[],
                descriptions=[],
                quantities=[],
            )
            current_cps.lines.append(line)
            current_line = line
        elif tag == 'PIA':
            if current_line is None:
                raise _fail('unexpected_segment', 'PIA 位于 LIN 之前', segment, segments)
            supplement = segment.elements[1] if len(segment.elements) > 1 else []
            current_line.additional.append({
                'qualifier': component(segment.elements, 0, 0, ''),
                'item': supplement[0] if supplement else '',
                'components': supplement,
                'source': segment.source_ref(),
            })
        elif tag == 'IMD':
            if current_line is None:
                raise _fail('unexpected_segment', 'IMD 位于 LIN 之前', segment, segments)
            description_element = segment.elements[2] if len(segment.elements) > 2 else []
            current_line.descriptions.append({
                'text': component(segment.elements, 2, 3, ''),
                'components': description_element,
                'source': segment.source_ref(),
            })
        elif tag == 'QTY':
            if current_line is None:
                raise _fail('unexpected_segment', 'QTY 位于 LIN 之前', segment, segments)
            current_line.quantities.append(
                _parse_quantity(
                    segment, segments, current_cps, current_line, syntax
                )
            )
        elif tag in EXCHANGE_TAGS:
            raise _fail('unexpected_segment', '消息正文含包络段', segment, segments)
        else:
            unknown.append(segment)

    if bgm is None:
        raise StructureError(
            'missing_bgm', '消息缺少 BGM',
            body[0].byte_start if body else unt.byte_start,
            segments, body[0] if body else unt,
        )
    if not nodes:
        raise StructureError(
            'missing_cps', '消息缺少 CPS',
            body[0].byte_start if body else unt.byte_start,
            segments, body[0] if body else unt,
        )

    # 全部收集后再解析父子关系：父节点可以在子节点之后出现。
    roots = []
    for cps_id, node in nodes.items():
        if node.parent_id is None:
            roots.append(node)
            continue
        parent = nodes.get(node.parent_id)
        if parent is None:
            raise _fail('missing_parent', 'CPS 父编号不存在', node.segment, segments)
        if parent is node:
            raise _fail('cps_cycle', 'CPS 以自身为父', node.segment, segments)
        parent.children.append(node)
        node.parent = parent

    for node in nodes.values():
        ancestors = set()
        walker = node
        while walker is not None:
            if id(walker) in ancestors:
                raise _fail('cps_cycle', 'CPS 父子关系成环', node.segment, segments)
            ancestors.add(id(walker))
            walker = walker.parent
    if not roots:
        node = next(iter(nodes.values()))
        raise _fail('cps_cycle', 'CPS 父子关系成环且无根', node.segment, segments)

    return Message(
        unh=unh,
        unt=unt,
        control_reference=None,
        message_reference=message_ref,
        syntax_version=version,
        bgm=bgm,
        nodes=nodes,
        roots=roots,
        unknown=unknown,
    )
