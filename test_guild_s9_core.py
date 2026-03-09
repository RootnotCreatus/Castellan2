import unittest
from guild_s9_core import build_level_state, calculate_master_grant_cost_tenths, calculate_sealconvert_lux

class TestGuildS9Core(unittest.TestCase):
    def test_build_level_state_zero(self):
        state=build_level_state(0)
        self.assertEqual(state.level, 0)
        self.assertEqual(state.title, 'без звания')

    def test_build_level_state_threshold(self):
        state=build_level_state(1000)
        self.assertEqual(state.level, 10)
        self.assertEqual(state.title, 'Ученик')

    def test_master_grant_cost_lux(self):
        self.assertEqual(calculate_master_grant_cost_tenths('lux', 100), 10)
        self.assertEqual(calculate_master_grant_cost_tenths('lux', 1), 1)

    def test_master_grant_cost_xp(self):
        self.assertEqual(calculate_master_grant_cost_tenths('xp', 1000), 10)
        self.assertEqual(calculate_master_grant_cost_tenths('xp', 50), 1)

    def test_sealconvert(self):
        gross, fee, net = calculate_sealconvert_lux(10, fee_percent=8)
        self.assertEqual(gross, 100)
        self.assertEqual(fee, 8)
        self.assertEqual(net, 92)

if __name__ == '__main__':
    unittest.main()
