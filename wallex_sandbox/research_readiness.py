"""Assess eligibility for a FUTURE PAPER test, never for real-money execution."""


def assess(validation,check,recent,matched_check=None,matched_recent=None):
    reasons=[]
    for label,record,minimum in (("validation",validation,10),("check",check,20),("recent",recent,5)):
        if record is None:
            reasons.append(label+":missing")
            continue
        base,stress=record["base"],record["stress"]
        if base["trades"]<minimum:
            reasons.append(label+":insufficient_completed_trades")
        if base["net_pnl"]<=0:
            reasons.append(label+":nonpositive_net_pnl")
        if stress["net_pnl"]<=0:
            reasons.append(label+":nonpositive_stressed_net_pnl")
        if base["profit_factor"] is not None and base["profit_factor"]<1.1:
            reasons.append(label+":weak_profit_factor")
        if max(base["max_drawdown_pct"],stress["max_drawdown_pct"])>=2:
            reasons.append(label+":drawdown_limit")
    for label,record,control in (("check",check,matched_check),("recent",recent,matched_recent)):
        if record is not None and control is not None and record["base"]["net_pnl"]<=control["base"]["net_pnl"]:
            reasons.append(label+":does_not_beat_matched_control")
    return {"approved_for_paper_forward_test":not reasons,"real_money_enabled":False,"rejections":reasons,
            "scope":"Historical research gate only; not evidence of prospective profitability or verified execution"}
