"""DESADV 采集：分块、层级、包络与提交边界。"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from source_data.edifact import DEFAULT_SYNTAX, EdifactError, detect_syntax, scan
from source_data.ingest import ingest, jsonable
from source_data.io import load

DATA = Path(__file__).resolve().parent.parent / 'data'


def build(config=None, *, una=True, items=None, exc=1):
    """用默认服务字符构造一个交换；items 是每消息的正文段列表。"""
    if items is None:
        items = [[
            'BGM+351+SHIP1',
            'CPS+2+1',
            'PAC+1++CT',
            'LIN+1++SKU-A:SA',
            "IMD+F++:::Service?+Pack?'One",
            'QTY+12:2.50',
            'CPS+1',
            'PAC+1++PL',
        ]]
    head = "UNA:+.? '" if una else ''
    parts = [head, 'UNB+UNOC:3+PARTNER+WAREHOUSE+261111:1200+X%03d' % exc]
    for index, body in enumerate(items, 1):
        ref = 'M%d' % index
        parts.append('UNH+%s+DESADV:D:96A:UN' % ref)
        parts.extend(body)
        parts.append('UNT+%d+%s' % (len(body) + 2, ref))
    parts.append('UNZ+%d+X%03d' % (len(items), exc))
    body = ''.join(
        part if index == 0 and una else part + "'"
        for index, part in enumerate(parts) if part
    )
    return body.encode('iso-8859-1')


def config(**overrides):
    base = {
        'partner': 'partner-demo',
        'charset': 'iso-8859-1',
        'chunk_size': 7,
        'max_segment_bytes': 1048576,
    }
    base.update(overrides)
    return base


def run(raw, **overrides):
    return ingest(config(**overrides), raw, store={})


def message(raw):
    return run(raw)['records'][0]['message']


class ChunkingTests(unittest.TestCase):
    def test_same_records_for_any_chunk_size(self):
        raw = build()
        expected = run(raw)['records']
        for size in (1, 2, 3, 7, 64, len(raw), len(raw) + 10):
            with self.subTest(size=size):
                self.assertEqual(run(raw, chunk_size=size)['records'], expected)

    def test_release_across_chunk_boundary(self):
        raw = build()
        chunks = []
        for size in range(1, len(raw) + 1):
            segments = scan(raw, chunk_size=size)[1]
            chunks.append([(s.ordinal, s.byte_start, s.byte_end) for s in segments])
        self.assertTrue(all(rows == chunks[0] for rows in chunks))

    def test_escaped_terminator_does_not_split(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', 'LIN+1++SKU-A:SA',
            "IMD+F++:::A?'B?+C",
        ]])
        result = run(raw)
        self.assertEqual(result['status'], 'accepted')
        line = message(raw)['roots'][0]['lines'][0]
        self.assertEqual(line['descriptions'][0]['text'], "A'B+C")
        self.assertIn("A?'B?+C", line['descriptions'][0]['source']['raw'])


class ErrorBoundaryTests(unittest.TestCase):
    def test_truncated_exchange_is_isolated(self):
        raw = (DATA / 'truncated.edi').read_bytes()
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['counts']['accepted'], 0)
        record = result['records'][0]
        self.assertEqual(record['state'], 'isolated')
        self.assertEqual(record['errors'][0]['code'], 'unclosed_segment')
        self.assertEqual(record['errors'][0]['byte_offset'], 193)
        self.assertGreater(len(record['known_segments']), 5)
        self.assertNotIn('message', record)

    def test_illegal_byte_reports_offset(self):
        raw = build().replace(b'SKU-A', b'SK\x00-A')
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        offset = result['records'][0]['errors'][0]['byte_offset']
        self.assertEqual(raw[offset], 0)

    def test_dangling_release_at_eof(self):
        raw = build() + b'?'
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        error = result['records'][0]['errors'][0]
        self.assertEqual(error['code'], 'dangling_release')
        self.assertEqual(error['byte_offset'], len(raw) - 1)

    def test_release_may_escape_the_next_terminator_lexically(self):
        raw = build(items=[['BGM+351+SHIP1', 'CPS+1', "LIN+1++SKU-A:SA?"]])
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        # ?U 让 UNT 变成 LIN 的内容，于是消息没有 UNT；这不是文件末悬挂释放。
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'missing_unt')

    def test_one_bad_message_isolates_whole_exchange(self):
        raw = build(items=[
            ['BGM+351+SHIP1', 'CPS+1', 'PAC+1++PL'],
            ['BGM+351+SHIP1', 'CPS+2+9'],
        ])
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['counts']['accepted'], 0)
        self.assertEqual(result['counts']['rejected_messages'], 2)
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'missing_parent')
        self.assertEqual(len(result['records'][0]['message_references']), 2)

    def test_unclosed_segment_before_unz(self):
        raw = b"UNA:+.? 'UNB+UNOC:3+P+W+1:1+C1'UNZ+1+C1"
        self.assertEqual(run(raw)['status'], 'isolated')
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'], 'unclosed_segment')


class SyntaxTests(unittest.TestCase):
    def test_defaults_without_una(self):
        raw = build(una=False)
        parsed = detect_syntax(raw)
        self.assertEqual(parsed, DEFAULT_SYNTAX)
        self.assertEqual(run(raw)['status'], 'accepted')

    def test_custom_una_redefines_all_service_characters(self):
        raw = (
            "UNA|^!% #"
            "UNB^UNOC|3^PARTNER^WAREHOUSE^261111|1200^C1#"
            "UNH^M1^DESADV|D|96A|UN#"
            "BGM^351^SHIP1#CPS^1#"
            "LIN^1^^SKU-A|SA#"
            "IMD^F^^^|||Service%^Pack%'One#"
            "QTY^12|2!5#"
            "UNT^7^M1#UNZ^1^C1#"
        ).encode('iso-8859-1')
        result = run(raw)
        self.assertEqual(result['status'], 'accepted')
        syntax = result['records'][0]['source']['syntax']
        self.assertEqual(
            (syntax['component'], syntax['element'], syntax['decimal'],
             syntax['release'], syntax['reserved'], syntax['terminator']),
            ('|', '^', '!', '%', ' ', '#'),
        )
        quantities = message(raw)['roots'][0]['lines'][0]['quantities']
        self.assertEqual(quantities[0]['raw'], '2!5')
        self.assertEqual(quantities[0]['value'], Decimal('2.5'))

    def test_duplicate_service_characters_rejected(self):
        raw = build()
        raw = b'UNA:++? ' + raw[9:]
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'invalid_una')

    def test_reserved_must_be_space(self):
        raw = b'UNA:+.?X' + build()[9:]
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['records'][0]['errors'][0]['byte_offset'], 7)

    def test_syntax_version_4_rejected(self):
        raw = build().replace(b'UNOC:3', b'UNOC:4')
        result = run(raw)
        self.assertEqual(result['records'][0]['errors'][0]['code'],
                         'unsupported_syntax_version')

    def test_custom_chunking_still_stable(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', "LIN+1++A:SA", "IMD+F++:::x?'y",
        ]], una=False)
        expected = run(raw)['records']
        for size in range(1, 12):
            self.assertEqual(run(raw, chunk_size=size)['records'], expected)


class EnvelopeTests(unittest.TestCase):
    def test_unt_count_must_include_envelope_segments(self):
        raw = build().replace(b'UNT+10+M1', b'UNT+9+M1')
        result = run(raw)
        self.assertEqual(result['records'][0]['errors'][0]['code'],
                         'segment_count_mismatch')

    def test_unz_message_count(self):
        raw = build().replace(b'UNZ+1+X001', b'UNZ+2+X001')
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'],
                         'message_count_mismatch')

    def test_control_reference_must_match(self):
        raw = build().replace(b'UNZ+1+X001', b'UNZ+1+X002')
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'],
                         'control_reference_mismatch')

    def test_message_reference_must_match(self):
        raw = build().replace(b'UNT+10+M1', b'UNT+10+M2')
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'],
                         'message_reference_mismatch')

    def test_unexpected_segment_between_messages(self):
        raw = build(items=[['BGM+351+SHIP1', 'CPS+1'],
                           ['BGM+351+SHIP1', 'CPS+1']])
        raw = raw.replace(b"'UNH+M2", b"'FTX+note'UNH+M2")
        result = run(raw)
        self.assertEqual(result['records'][0]['errors'][0]['code'],
                         'unexpected_segment')


class StructureTests(unittest.TestCase):
    def test_parent_appearing_after_child(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+2+1', 'PAC+1++CT',
            'LIN+1++SKU-A:SA', 'QTY+12:2.50', 'CPS+1', 'PAC+1++PL',
        ]])
        payload = message(raw)
        self.assertEqual(len(payload['roots']), 1)
        root = payload['roots'][0]['id']
        self.assertEqual(root, '1')
        child = payload['roots'][0]['children'][0]
        self.assertEqual(child['id'], '2')
        self.assertEqual(child['lines'][0]['item_number'], 'SKU-A')
        self.assertEqual(child['lines'][0]['quantities'][0]['shipped'], True)
        self.assertEqual(child['parent']['tag'], 'CPS')

    def test_multiple_roots(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', 'PAC+1++PL', 'CPS+2', 'PAC+2++CT',
        ]])
        payload = message(raw)
        self.assertEqual([node['id'] for node in payload['roots']], ['1', '2'])

    def test_duplicate_cps_rejected(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', 'PAC+1++PL', 'CPS+1', 'PAC+1++CT',
        ]])
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'],
                         'duplicate_cps')

    def test_missing_parent_rejected(self):
        raw = build(items=[['BGM+351+SHIP1', 'CPS+2+9']])
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'],
                         'missing_parent')

    def test_cycle_rejected(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1+2', 'CPS+2+1',
        ]])
        self.assertEqual(run(raw)['records'][0]['errors'][0]['code'], 'cps_cycle')

    def test_current_line_does_not_leak_between_messages(self):
        raw = build(items=[
            ['BGM+351+SHIP1', 'CPS+1', 'LIN+1++SKU-A:SA', 'QTY+12:1'],
            ['BGM+351+SHIP1', 'CPS+1', 'QTY+12:1'],
        ])
        result = run(raw)
        self.assertEqual(result['status'], 'isolated')
        self.assertEqual(result['records'][0]['errors'][0]['code'],
                         'unexpected_segment')

    def test_quantity_assigned_to_current_line_and_cps(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', 'PAC+1++PL', 'LIN+1++A:SA',
            'QTY+12:2.50', 'QTY+38:3.00',
            'CPS+2+1', 'LIN+2++B:SA', 'QTY+12:0',
        ]])
        payload = message(raw)
        first = payload['roots'][0]['lines'][0]
        self.assertEqual(len(first['quantities']), 2)
        self.assertEqual(first['quantities'][0]['raw'], '2.50')
        self.assertEqual(first['quantities'][0]['value'], Decimal('2.50'))
        self.assertEqual(first['quantities'][1]['raw'], '3.00')
        self.assertFalse(first['quantities'][1]['shipped'])

    def test_quantity_lexical_and_decimal_value_both_kept(self):
        payload = message(build())
        quantity = payload['roots'][0]['children'][0]['lines'][0]['quantities'][0]
        self.assertEqual(quantity['raw'], '2.50')
        self.assertEqual(quantity['value'], Decimal('2.50'))
        self.assertEqual(str(jsonable(quantity)['value']), '2.50')
        self.assertEqual(quantity['qualifier'], '12')

    def test_quantity_rules(self):
        for text, code in (('-1', 'invalid_qty'), ('2.5.5', 'invalid_qty'),
                          ('abc', 'invalid_qty'), ('', 'invalid_qty'),
                          ('1e3', 'invalid_qty'), (' 2', 'invalid_qty')):
            raw = build(items=[['BGM+351+SHIP1', 'CPS+1',
                                'LIN+1++A:SA', 'QTY+12:%s' % text]])
            result = run(raw)
            self.assertEqual(result['records'][0]['errors'][0]['code'], code)

    def test_pac_count_integer(self):
        raw = build(items=[['BGM+351+SHIP1', 'CPS+1', 'PAC+1.5++PL']])
        result = run(raw)
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'invalid_pac')

    def test_empty_components_preserved(self):
        raw = build(items=[['BGM+351+SHIP1', 'CPS+1',
                            'LIN+1++A:SA', 'IMD+F++:::']])
        element = message(raw)['roots'][0]['lines'][0]['descriptions'][0]
        self.assertEqual(element['components'], ['', '', '', ''])

    def test_unknown_segment_preserved(self):
        raw = build(items=[[
            'BGM+351+SHIP1', 'CPS+1', 'FTX+AAI+++free text', 'PAC+1++PL',
        ]])
        payload = message(raw)
        unknown = payload['unknown'][0]
        self.assertEqual(unknown['segment']['tag'], 'FTX')
        self.assertEqual(unknown['segment']['raw'], "FTX+AAI+++free text'")
        self.assertEqual(unknown['elements'], [['AAI'], [''], [''], ['free text']])
        self.assertNotIn(unknown, payload['roots'])

    def test_missing_bgm_and_cps(self):
        result = run(build(items=[['CPS+1']]))
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'missing_bgm')
        result = run(build(items=[['BGM+351+SHIP1', 'PAC+1++PL']]))
        self.assertEqual(result['records'][0]['errors'][0]['code'], 'unexpected_segment')


class IdentityTests(unittest.TestCase):
    def test_same_identity_same_original_is_idempotent(self):
        raw = build()
        store = {}
        first = ingest(config(), raw, store=store)
        second = ingest(config(), raw, store=store)
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(second['status'], 'unchanged')
        self.assertEqual(second['counts']['accepted'], 0)
        self.assertEqual(len(store), 1)

    def test_same_identity_different_original_conflicts_without_overwrite(self):
        raw = build()
        store = {}
        ingest(config(), raw, store=store)
        changed = raw.replace(b'Service?+Pack?', b'Service?+Box?')
        result = ingest(config(), changed, store=store)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['counts']['conflicts'], 1)
        self.assertEqual(result['counts']['accepted'], 0)
        record_id = result['conflicts'][0]['record_id']
        self.assertIn(record_id, store)
        self.assertIn('Service+Pack',
                      json.dumps(jsonable(store[record_id]), ensure_ascii=False))

    def test_identity_uses_partner_and_both_references(self):
        raw = build()
        store = {}
        ingest(config(partner='p1'), raw, store=store)
        result = ingest(config(partner='p2'), raw, store={})
        self.assertEqual(result['status'], 'accepted')
        self.assertNotEqual(
            result['records'][0]['record_id'],
            next(iter(store.values()))['record_id'],
        )

    def test_different_chunk_size_same_identity(self):
        raw = build()
        store = {}
        ingest(config(chunk_size=1), raw, store=store)
        result = ingest(config(chunk_size=len(raw)), raw, store=store)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(len(store), 1)

    def test_truncated_isolated_is_idempotent_but_not_ready(self):
        raw = (DATA / 'truncated.edi').read_bytes()
        cfg = load(DATA / 'truncated-envelope.json')[0]
        store = {}
        first = ingest(cfg, raw, store=store)
        second = ingest(cfg, raw, store=store)
        self.assertEqual(first['status'], 'isolated')
        self.assertEqual(second['status'], 'unchanged')
        self.assertNotIn('message', store[next(iter(store))])

    def test_isolated_record_keeps_known_segments_and_digest(self):
        raw = (DATA / 'truncated.edi').read_bytes()
        cfg = load(DATA / 'truncated-envelope.json')[0]
        record = ingest(cfg, raw, store={})['records'][0]
        self.assertEqual(record['exchange']['sha256'], cfg['source']['sha256'])
        self.assertEqual(record['exchange']['raw'], raw.decode('iso-8859-1'))
        tags = [segment['tag'] for segment in record['known_segments']]
        self.assertEqual(tags[-1], 'UNT')

    def test_sample_sources_round_trip(self):
        for name in ('normal.json', 'release-byte-chunks.json',
                     'truncated-envelope.json'):
            with self.subTest(file=name):
                cfg, _ = load(DATA / name)
                raw = (DATA / cfg['source']['path']).read_bytes()
                self.assertEqual(len(raw), cfg['source']['bytes'])
                result = ingest(cfg, raw, store={})
                self.assertIn(result['status'], ('accepted', 'isolated'))

    def test_input_config_rejected(self):
        raw = build()
        for overrides in ({'charset': 'utf-8'}, {'chunk_size': 0},
                          {'chunk_size': True}, {'max_segment_bytes': 0},
                          {'partner': ''}):
            with self.subTest(overrides=overrides):
                cfg = config(**overrides)
                with self.assertRaises(ValueError):
                    ingest(cfg, raw, store={})


if __name__ == '__main__':
    unittest.main()
