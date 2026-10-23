# Questions to resolve with the organizers

These are drafts for the team to send; no messages have been sent.

## Blocking before live deployment

1. **Official time window:** What are the exact start/end timestamps, timezone, and video/code submission deadline? Are orders outside the three-day interval excluded or prohibited?
2. **Final settlement conflict:** `wallex-p1.txt` lines 134–141 include residual assets and unrealized P&L in final equity, but line 142 requires everything realized in toman. Must all positions and USDT balances be converted to TMN before the cutoff? How are dust, unfilled sells, suspended markets, and fees assessed?
3. **Credit-account routing:** Please confirm the issued credential is for the challenge credit account, provide the correct `sub-account-client-id` or read-only discovery route, and confirm the initial funded equity. Is there a challenge-specific API host, fee tier, financing charge, or account expiry?
4. **Allowed markets:** Is the supplied exclusion list authoritative for this specific credit package? Can we retrieve account-specific restrictions through the API? How are mid-event restriction changes handled, including exits from existing positions?

## Scoring and technical acceptance

5. Is the proposed 300-point table final? Is the winner determined by total points, financial ranking subject to technical approval, or a financial shortlist followed by total points? What are the tie-breaks?
6. How are drawdown, speed, stability, and reasonable activity measured? Is there a minimum number of trades or active periods? We will not create artificial activity.
7. Does a learned predictive model plus an autonomous tool-using decision agent satisfy the AI/Agentic AI requirement? Are an LLM, Wallex MCP, or particular AI tools mandatory? Is deterministic replay of recorded external-model outputs accepted?
8. May an operator stop/restart the system, activate a kill switch, deploy a versioned bug fix, or change a versioned configuration? Which interventions must be reported? How should emergency liquidation be performed while respecting the ban on manual trades?
9. Must the delivered repository contain every version that traded, with a deployment manifest, or only the final version? How should judges receive access to the private repository, market datasets, and sanitized decision logs?
10. Where and how should the mandatory approximately five-minute video and other artifacts be submitted?

## Document correction

The example with initial equity 5,000,000 TMN and final equity 6,250,000 TMN has profit of **1,250,000 TMN** and return of **25%**. The printed profit of 1,500,000 TMN appears to be an arithmetic error. Please confirm the stated return formula is authoritative.

The settlement examples using 2,000,000 TMN appear illustrative; the repeatedly stated challenge baseline is 5,000,000 TMN. Confirm if this team's actual funded baseline differs.
