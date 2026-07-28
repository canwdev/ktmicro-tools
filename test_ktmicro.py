"""Round-trip tests for KT02H20 encoding/decoding functions."""

import unittest

from kt02h20 import (
    _ng_byte_db, _ng_byte_to_db,
    ng_encode_71, ng_decode_71,
    ng_encode_72, ng_decode_72,
    ng_encode_73, ng_decode_73,
    lim_encode_78, lim_decode_78,
    lim_encode_79, lim_decode_79,
    eq_encode_a, eq_encode_b, _eq_decode_a, _eq_decode_b,
    dig_encode, dig_decode,
    FILTER_TYPES, PGA_ADC_GAINS, PGA_DAC_GAINS,
)
from ktmicro import _pga_resolve


class TestNgByteEncoding(unittest.TestCase):
    def test_round_trip(self):
        for dB in range(-256, 0):
            self.assertEqual(_ng_byte_to_db(_ng_byte_db(dB)), dB)

    def test_boundary_valid(self):
        self.assertEqual(_ng_byte_db(-256), 0)
        self.assertEqual(_ng_byte_db(-1), 255)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            _ng_byte_db(0)
        with self.assertRaises(ValueError):
            _ng_byte_db(-257)


class TestNgRegisters(unittest.TestCase):
    def test_encode_decode_71(self):
        v = ng_encode_71(True, -40, -30, -50)
        d = ng_decode_71(v)
        self.assertTrue(d['en'])
        self.assertEqual(d['gatevol'], -40)
        self.assertEqual(d['thhigh'], -30)
        self.assertEqual(d['thlow'], -50)

    def test_encode_decode_71_disabled(self):
        v = ng_encode_71(False, -60, -20, -80)
        d = ng_decode_71(v)
        self.assertFalse(d['en'])
        self.assertEqual(d['gatevol'], -60)

    def test_encode_decode_72(self):
        v = ng_encode_72(100, 50)
        d = ng_decode_72(v)
        self.assertEqual(d['rt'], 100)
        self.assertEqual(d['at'], 50)

    def test_encode_decode_73(self):
        v = ng_encode_73(200)
        d = ng_decode_73(v)
        self.assertEqual(d['noiset'], 200)
        self.assertEqual(d['hold'], 0x000A)


class TestLimRegisters(unittest.TestCase):
    def test_encode_decode_78(self):
        v = lim_encode_78(True, False, -6)
        d = lim_decode_78(v)
        self.assertTrue(d['en'])
        self.assertFalse(d['soft'])
        self.assertEqual(d['point'], -6)

    def test_encode_decode_78_soft(self):
        v = lim_encode_78(False, True, -12)
        d = lim_decode_78(v)
        self.assertFalse(d['en'])
        self.assertTrue(d['soft'])
        self.assertEqual(d['point'], -12)

    def test_encode_decode_79(self):
        v = lim_encode_79(150, 75)
        d = lim_decode_79(v)
        self.assertEqual(d['rt'], 150)
        self.assertEqual(d['at'], 75)


class TestEqEncoding(unittest.TestCase):
    def test_encode_a_round_trip(self):
        va = eq_encode_a(1000, 3.5)
        freq, gain = _eq_decode_a(va)
        self.assertEqual(freq, 1000)
        self.assertAlmostEqual(gain, 3.5, places=1)

    def test_encode_a_negative_gain(self):
        va = eq_encode_a(500, -6.0)
        freq, gain = _eq_decode_a(va)
        self.assertEqual(freq, 500)
        self.assertAlmostEqual(gain, -6.0, places=1)

    def test_encode_b_all_types(self):
        for ftype_int, ftype_str in FILTER_TYPES.items():
            vb = eq_encode_b(ftype_int, 1.0)
            ft, q = _eq_decode_b(vb)
            self.assertEqual(ft, ftype_int)
            self.assertAlmostEqual(q, 1.0, places=2)

    def test_encode_decode_ab(self):
        va = eq_encode_a(2000, -3.0)
        vb = eq_encode_b(2, 0.5)
        freq, gain = _eq_decode_a(va)
        ft, q = _eq_decode_b(vb)
        self.assertEqual(freq, 2000)
        self.assertAlmostEqual(gain, -3.0, places=1)
        self.assertEqual(ft, 2)
        self.assertAlmostEqual(q, 0.5, places=2)


class TestDigGain(unittest.TestCase):
    def test_round_trip_positive(self):
        for db10 in range(0, 1280, 5):
            db = db10 / 10.0
            if db <= 63.5:
                self.assertAlmostEqual(dig_decode(dig_encode(db)), db, places=1)

    def test_round_trip_negative(self):
        for db10 in range(-640, 0, 5):
            db = db10 / 10.0
            self.assertAlmostEqual(dig_decode(dig_encode(db)), db, places=1)

    def test_zero(self):
        self.assertAlmostEqual(dig_decode(dig_encode(0.0)), 0.0)

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            dig_encode(64.0)
        with self.assertRaises(ValueError):
            dig_encode(-65.0)


class TestPgaResolve(unittest.TestCase):
    def test_adc_db_match(self):
        self.assertEqual(_pga_resolve(PGA_ADC_GAINS, '0'), 0)
        self.assertEqual(_pga_resolve(PGA_ADC_GAINS, '14'), 3)
        self.assertEqual(_pga_resolve(PGA_ADC_GAINS, '44'), 7)

    def test_dac_db_match(self):
        self.assertEqual(_pga_resolve(PGA_DAC_GAINS, '-18'), 1)
        self.assertEqual(_pga_resolve(PGA_DAC_GAINS, '3'), 15)

    def test_mute(self):
        self.assertEqual(_pga_resolve(PGA_DAC_GAINS, 'mute'), 0)

    def test_mute_not_in_adc(self):
        with self.assertRaises(ValueError):
            _pga_resolve(PGA_ADC_GAINS, 'mute')

    def test_raw_index(self):
        self.assertEqual(_pga_resolve(PGA_ADC_GAINS, '5'), 5)

    def test_index_out_of_range(self):
        with self.assertRaises(ValueError):
            _pga_resolve(PGA_ADC_GAINS, '99')

    def test_invalid_string(self):
        with self.assertRaises(ValueError):
            _pga_resolve(PGA_ADC_GAINS, 'abc')

    def test_no_matching_db_falls_to_index(self):
        # '5' doesn't match any dB value but is a valid index
        self.assertEqual(_pga_resolve(PGA_ADC_GAINS, '5'), 5)


if __name__ == '__main__':
    unittest.main()
