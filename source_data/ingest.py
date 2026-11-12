"""采集一个 EDIFACT 交换，并在全部核对通过后提交可见记录。

提交边界是整个交换：扫描、包络或任一消息结构失败时，只产生一条隔离
记录，不会把已解析部分当作完整收货记录。已经提交的记录存放在传入的
内存存储中；相同身份和相同原文幂等，相同身份不同原文标记冲突。
"""
from decimal import Decimal
import hashlib

from .edifact import EdifactError, detect_syntax, scan
from .envelope import check_exchange, component
from .parse import parse_message


SUPPORTED_TAGS = ('BGM', 'CPS', 'PAC', 'LIN', 'PIA', 'IMD', 'QTY')


def _digest(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def identity_id(partner, control_reference, message_reference):
    """重采身份只由配置的伙伴和两级控制引用组成。"""
    material = '\x00'.join((partner, control_reference, message_reference))
    return _digest(material)


def exchange_id(partner, control_reference, exchange_sha256):
    """无法定位消息引用时，隔离记录的查找键带交换原文摘要。"""
    material = '\x00'.join((partner, control_reference, exchange_sha256))
    return _digest(material)


def jsonable(value):
    """把 Decimal 等非 JSON 值转成可序列化形式，不丢失原词法。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _unb_control(segments):
    for segment in segments:
        if segment.tag == 'UNB' and len(segment.elements) > 4:
            value = component(segment.elements, 4, 0)
            if value:
                return value
    return None


def _message_reference(segments):
    for segment in segments:
        if segment.tag == 'UNH' and segment.elements:
            value = component(segment.elements, 0, 0)
            if value:
                return value
    return None


def _known_segments(segments):
    return [
        {
            'segment_ordinal': segment.ordinal,
            'byte_start': segment.byte_start,
            'byte_end': segment.byte_end,
            'tag': segment.tag,
            'elements': segment.elements,
            'raw': segment.raw,
            'raw_sha256': segment.raw_sha256,
        }
        for segment in segments
    ]


def _source_block(path, config, raw, syntax):
    block = {
        'path': path,
        'charset': config.get('charset'),
        'byte_length': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'syntax': syntax.as_dict() if syntax is not None else None,
    }
    return block


def _isolate(config, raw, path, segments, syntax, errors, unb_ref, message_refs):
    reference = message_refs[0] if len(message_refs) == 1 else None
    unb = _unb_control(segments)
    if reference is not None and unb is not None:
        record_id = identity_id(config['partner'], unb, reference)
    else:
        record_id = exchange_id(
            config['partner'], unb or '', hashlib.sha256(raw).hexdigest()
        )
    envelope_refs = {}
    for tag in ('UNB', 'UNZ'):
        for segment in segments:
            if segment.tag == tag:
                envelope_refs[tag] = segment.source_ref()
                break
    record = {
        'record_id': record_id,
        'state': 'isolated',
        'identity': {
            'partner': config['partner'],
            'unb_control_reference': unb,
            'message_reference': reference,
            'message_references': message_refs,
        },
        'source': _source_block(path, config, raw, syntax),
        'exchange': {
            'sha256': hashlib.sha256(raw).hexdigest(),
            'syntax': syntax.as_dict() if syntax is not None else None,
            'unb': envelope_refs.get('UNB'),
            'unz': envelope_refs.get('UNZ'),
            'raw': raw.decode('iso-8859-1'),
        },
        'errors': [error.as_dict() for error in errors],
        'known_segments': _known_segments(segments),
        'message_references': message_refs,
    }
    return record


def message_span_sha256(raw, message):
    """消息本文（UNH 起点到 UNT 终点）的原文摘要。

    身份和幂等都以这段原字节为依据，不用重构或规范化后的字段。
    """
    return hashlib.sha256(raw[message.unh.byte_start:message.unt.byte_end]).hexdigest()


def _message_record(config, raw, path, syntax, envelope, message):
    unb_control = component(envelope.unb.elements, 4, 0)
    payload = message.as_dict()
    payload['control_reference'] = unb_control
    payload['sha256'] = message_span_sha256(raw, message)
    record = {
        'record_id': identity_id(
            config['partner'], unb_control, message.message_reference
        ),
        'state': 'ready',
        'identity': {
            'partner': config['partner'],
            'unb_control_reference': unb_control,
            'message_reference': message.message_reference,
        },
        'source': _source_block(path, config, raw, syntax),
        'exchange': {
            'sha256': hashlib.sha256(raw).hexdigest(),
            'syntax': syntax.as_dict(),
            'unb': envelope.unb.source_ref(),
            'unz': envelope.unz.source_ref(),
        },
        'message': payload,
    }
    return record


def _same_original(existing, candidate):
    """同身份是否同原文：优先比消息本文摘要，再退到整个交换摘要。"""
    if (
        existing.get('state') == 'ready'
        and candidate.get('state') == 'ready'
        and existing.get('message', {}).get('sha256')
        and candidate.get('message', {}).get('sha256')
    ):
        return (
            existing['message']['sha256'] == candidate['message']['sha256']
        )
    return (
        existing.get('exchange', {}).get('sha256')
        == candidate.get('exchange', {}).get('sha256')
    )


def _incoming_sha(record):
    if record.get('state') == 'ready':
        return record.get('message', {}).get('sha256')
    return record.get('exchange', {}).get('sha256')


def _record_digest(record):
    return record['exchange']['sha256']


def ingest(config, raw, *, path=None, store=None):
    """核对并提交一个交换。

    store 为可选的 dict；成功记录写入其中，隔离记录也写入以便保留证据，
    冲突不覆盖已有记录。返回带 counts 的结果。
    """
    if store is None:
        store = {}
    if not isinstance(store, dict):
        raise ValueError('存储应为字典')
    partner = config.get('partner')
    if not isinstance(partner, str) or not partner:
        raise ValueError('伙伴身份缺失')
    if config.get('charset') != 'iso-8859-1':
        raise ValueError('字符集不在清单范围')
    chunk_size = config.get('chunk_size')
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError('chunk_size 必须为正整数')
    max_segment_bytes = config.get('max_segment_bytes')
    if type(max_segment_bytes) is not int or max_segment_bytes < 1:
        raise ValueError('max_segment_bytes 必须为正整数')

    raw = bytes(raw)
    syntax = None
    segments = []
    errors = []
    envelope = None
    parsed = []

    try:
        syntax, segments = scan(
            raw,
            chunk_size=chunk_size,
            max_segment_bytes=max_segment_bytes,
        )
        envelope = check_exchange(syntax, segments)
        for unh, body, unt in envelope.messages:
            try:
                parsed.append(parse_message(unh, unt, body, segments, syntax))
            except EdifactError as error:
                errors.append(error)
        if errors:
            parsed = []
    except EdifactError as error:
        errors.append(error)
        segments = error.segments
        parsed = []

    if errors and syntax is None:
        try:
            syntax = detect_syntax(raw)
        except EdifactError:
            syntax = None

    message_refs = [
        component(unh.elements, 0, 0)
        for unh, _body, _unt in (envelope.messages if envelope else [])
        if unh.elements
    ]
    if not message_refs:
        fallback = _message_reference(segments)
        if fallback:
            message_refs = [fallback]

    result = {
        'status': None,
        'records': [],
        'conflicts': [],
        'counts': {
            'messages_seen': len(message_refs),
            'accepted': 0,
            'unchanged': 0,
            'rejected_messages': 0,
            'isolated_exchanges': 0,
            'conflicts': 0,
        },
    }

    if errors:
        isolate = _isolate(
            config, raw, path, segments, syntax, errors,
            _unb_control(segments), message_refs,
        )
        result['counts']['rejected_messages'] = max(1, len(message_refs))
        result['counts']['isolated_exchanges'] = 1
        existing = store.get(isolate['record_id'])
        if existing is None:
            store[isolate['record_id']] = isolate
            result['records'].append(isolate)
            result['status'] = 'isolated'
        elif _same_original(existing, isolate):
            result['status'] = 'unchanged'
            result['counts']['unchanged'] += 1
        else:
            result['status'] = 'conflict'
            result['counts']['conflicts'] += 1
            result['conflicts'].append({
                'record_id': isolate['record_id'],
                'identity': isolate['identity'],
                'existing_sha': _incoming_sha(existing),
                'incoming_sha': _incoming_sha(isolate),
                'incoming_state': 'isolated',
                'incoming_errors': isolate['errors'],
            })
        return result

    candidates = [
        _message_record(config, raw, path, syntax, envelope, message)
        for message in parsed
    ]
    first = {}
    for candidate in candidates:
        first.setdefault(candidate['record_id'], candidate)

    # 同一次交换里若两条消息落到同一身份，也按身份不同原文处理。
    for record_id, candidate in first.items():
        if sum(1 for item in candidates if item['record_id'] == record_id) > 1:
            result['counts']['conflicts'] += 1
            result['conflicts'].append({
                'record_id': record_id,
                'identity': candidate['identity'],
                'reason': 'identity_repeated_in_exchange',
                'incoming_state': 'ready',
            })
        existing = store.get(record_id)
        if existing is None:
            continue
        if _same_original(existing, candidate):
            continue
        result['counts']['conflicts'] += 1
        result['conflicts'].append({
            'record_id': candidate['record_id'],
            'identity': candidate['identity'],
            'existing_state': existing.get('state'),
            'existing_sha': _incoming_sha(existing),
            'incoming_sha': _incoming_sha(candidate),
            'incoming_state': 'ready',
            'incoming_message': candidate['message'],
        })

    if result['conflicts']:
        result['status'] = 'conflict'
        return result

    for record_id, candidate in first.items():
        existing = store.get(record_id)
        if existing is None:
            store[record_id] = candidate
            result['records'].append(candidate)
            result['counts']['accepted'] += 1
        else:
            result['counts']['unchanged'] += 1

    if result['counts']['conflicts']:
        result['status'] = 'conflict'
    elif result['counts']['accepted']:
        result['status'] = 'accepted'
    else:
        result['status'] = 'unchanged'
    return result
