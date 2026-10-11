"""Regression fixtures are synthetic, never copied credentials/account data."""
import json
import math
import struct
import unittest
from unittest.mock import patch

import grok_quota_worker as g


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def message(field, payload):
    return varint(field * 8 + 2) + varint(len(payload)) + payload


def framed(payload, flags=0):
    return bytes([flags]) + len(payload).to_bytes(4, 'big') + payload


def period_only():
    # Complete, plausible billing period, but NOT a reported usage scalar.
    period = b'\x08\x02' + message(2, b'\x08' + varint(2_000_000_000))
    period += message(3, b'\x08' + varint(2_000_604_800))
    return message(1, message(8, period))


class GrokRefreshTests(unittest.TestCase):
    def test_period_only_protobuf_never_becomes_100_percent_remaining(self):
        result = g.parse_credits_response(period_only())
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'not_reported')
        self.assertEqual(result['reset_at'], 2_000_604_800)
        self.assertNotIn('limit', result)

    def test_period_only_json_is_successful_check_not_auth_failure(self):
        result = g._parse_cli_billing(json.dumps({'config': {
            'currentPeriod': {'type': 'USAGE_PERIOD_TYPE_WEEKLY',
                              'start': '2033-05-18T00:00:00Z', 'end': '2033-05-25T00:00:00Z'},
            'isUnifiedBillingUser': True, 'onDemandCap': {}, 'onDemandUsed': {}
        }}).encode())
        self.assertFalse(result['available'])
        self.assertEqual(result['status'], 'not_reported')
        self.assertNotIn('limit', result)

    def test_explicit_zero_is_not_missing(self):
        for value in (0, 0.25, 99.9, 100):
            result = g._parse_cli_billing(json.dumps({'config': {'creditUsagePercent': value}}).encode())
            self.assertTrue(result['available'])
            self.assertEqual(result['limit']['used_percent'], value)
        binary = message(1, b'\x0d' + struct.pack('<f', 0))
        self.assertEqual(g.parse_credits_response(binary)['limit']['remaining_percent'], 100)

    def test_rejects_invalid_percent_instead_of_clamping_to_healthy(self):
        for value in (None, True, 'bad', math.nan, math.inf, -1, 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                g._parse_cli_billing(json.dumps({'config': {'creditUsagePercent': value}}).encode())
        for value in (math.nan, math.inf, -1):
            with self.assertRaises(ValueError):
                g.parse_credits_response(message(1, b'\x0d' + struct.pack('<f', value)))

    def test_rejects_empty_config_and_error_objects(self):
        for payload in ({}, {'config': None}, {'config': {}}, {'error': 'not authorized'}):
            with self.assertRaises(ValueError):
                g._parse_cli_billing(json.dumps(payload).encode())
        with self.assertRaises(ValueError):
            g.parse_credits_response(message(1, b''))

    def test_grpc_checks_trailers_and_complete_frames(self):
        payload = framed(period_only())
        self.assertEqual(g._grpc_data_frames(payload + framed(b'grpc-status: 0\r\n', 128)), [period_only()])
        for bad in (payload + b'\x00', framed(b'abc', 1), framed(b'grpc-status: 16\r\n', 128), b'\x00\x00\x00\x00\x09x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                g._grpc_data_frames(bad)

    def test_varint_overflow_rejected(self):
        with self.assertRaises(ValueError):
            g._read_varint(b'\xff' * 9 + b'\x02')

    def test_unknown_fields_cannot_invent_percentage(self):
        payload = message(1, message(99, b'\x0d' + struct.pack('<f', 77)))
        with self.assertRaises(ValueError):
            g.parse_credits_response(payload)

    def test_refresh_can_recover_from_missing_to_reported(self):
        missing = json.dumps({'config': {'currentPeriod': {'start': '2033-05-18T00:00:00Z', 'end': '2033-05-25T00:00:00Z'}}}).encode()
        reported = message(1, b'\x0d' + struct.pack('<f', 42))
        with patch.object(g, '_request', side_effect=[missing, framed(reported)]):
            result = g.fetch_with_token('fixture')
        self.assertEqual(result['limit']['used_percent'], 42)

    def test_period_only_both_endpoints_stays_not_reported(self):
        missing = json.dumps({'config': {'currentPeriod': {'start': '2033-05-18T00:00:00Z', 'end': '2033-05-25T00:00:00Z'}}}).encode()
        with patch.object(g, '_request', side_effect=[missing, framed(period_only())]):
            result = g.fetch_with_token('fixture')
        self.assertEqual(result['status'], 'not_reported')
        self.assertNotIn('limit', result)


if __name__ == '__main__':
    unittest.main()
