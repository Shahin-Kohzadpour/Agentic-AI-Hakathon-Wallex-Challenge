import unittest

from .research_readiness import assess


def record(net=10,stress=8,trades=50,pf=1.5):
    return {"base":{"net_pnl":net,"trades":trades,"profit_factor":pf,"max_drawdown_pct":.5},
            "stress":{"net_pnl":stress,"max_drawdown_pct":.6}}


class ReadinessChecks(unittest.TestCase):
    def test_good_validation_does_not_excuse_a_failed_later_check(self):
        result=assess(record(),record(-3,-4,pf=.7),record())
        self.assertFalse(result["approved_for_paper_forward_test"])
        self.assertIn("check:nonpositive_net_pnl",result["rejections"])

    def test_abstention_is_not_counted_as_a_profitable_trading_sample(self):
        result=assess(record(),record(0,0,trades=0,pf=None),record())
        self.assertIn("check:insufficient_completed_trades",result["rejections"])
        self.assertFalse(result["approved_for_paper_forward_test"])

    def test_cost_stress_failure_blocks_paper_promotion(self):
        result=assess(record(),record(stress=-1),record())
        self.assertIn("check:nonpositive_stressed_net_pnl",result["rejections"])

    def test_passing_research_never_enables_real_money(self):
        result=assess(record(),record(),record(),record(2,1),record(2,1))
        self.assertTrue(result["approved_for_paper_forward_test"])
        self.assertFalse(result["real_money_enabled"])
