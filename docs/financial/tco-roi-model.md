# 3-year TCO / ROI and token economics

**Rubric:** Engineering Package (quantified 3-year TCO and ROI with token optimization strategies)

The live model is [`tco-roi-model.xlsx`](tco-roi-model.xlsx): every figure below is a formula over one Inputs sheet. Blue cells are inputs you can change. `scripts/tco_model.py` builds it and cross-checks its formulas against an independent Python computation (0 formula errors, 9/9 checkpoints equal).

Inputs follow the blueprint (`CapstoneProjectPlan_v2.md` §12, §17) with one correction: **the Year-0 build line uses the actual solo build (about 24 h of one developer working with Claude) instead of 10 engineers × 12 h.** Cloud prices are on-demand us-east-1 estimates for planning; validate them with the AWS Pricing Calculator before using them externally.

## Headline

| KPI | Value |
|---|---|
| **3-year net benefit** | **$361,580** |
| **NPV at 10 %** | **$272,400** |
| **Payback** | **month 14** of operation |
| Benefit-cost ratio | 2.71× |
| Net ROI | 171 % |
| SRE hours freed | 708 h in Y1 → 1,839 h in Y3 |
| LLM share of 3-year cost | 2.0 % |
| LLM cost per incident (as built) | $0.0645 (P1: $0.108) |

## Token economics: what the optimization strategies are worth

Average tokens per call: triage 8,192 in / 2,048 out, planner 4,096 / 3,072, evaluator summary 2,048 / 1,024. Prices per 1M tokens: Sonnet 5 $2 / $10, Haiku 4.5 $1 / $5. Assumes 40 % P1 incidents, a 20 % semantic-cache hit rate on triage + planning, and ×1.3 overhead for re-plans and retries.

| Strategy | $ per incident | 3-year LLM production cost | vs all-Sonnet |
|---|---|---|---|
| All Sonnet 5, no cache | $0.1171 | $1,122 | — |
| Tiered routing (Sonnet for P1 / tier-1, Haiku otherwise) | $0.0783 | $750 | −33 % |
| **Tiered routing + semantic cache (as built)** | **$0.0645** | **$617** | **−45 %** |

**What this means for the jury.** At 200–340 incidents a month, LLM spend is about 2 % of the platform's cost, so the optimizations save roughly $500 over three years. They matter for three other reasons:
1. **Scale:** the saving is linear in volume. At 10× the incidents, it's about $5,000.
2. **Latency:** a cache hit skips both LLM calls, the slowest steps in a live run. In the demo, a cache-hit triage finished 1.6 s after intake.
3. **Safety:** the cache only reuses *verified* fixes and caps confidence at 0.79, so it can't widen autonomy (ADR-003).

The ROI is driven by adoption and the automation mix, not by model prices. That's why the gate policy and confidence calibration matter more than squeezing tokens.

**Caveat:** the token counts are modeled, not measured. The live Claude path is built and tested with a fake client, but no live run has happened yet, because the API key was rejected. The token ledger (`/ledger/summary`) will replace these estimates with measured values once live runs are made.

## Costs

| Line ($ / month) | Y1 | Y2 | Y3 |
|---|---|---|---|
| Infrastructure (EKS, 3→5 app nodes, guard node, RDS Multi-AZ, ElastiCache, networking, observability) | $689 | $759 | $829 |
| LLM production (incidents × $0.0645) | $13 | $17 | $22 |
| LLM evaluation + staging | $100 | $100 | $100 |
| Platform maintenance (20 / 20 / 16 h × $150) | $3,000 | $3,000 | $2,400 |
| **Total per year** | **$45,623** | **$46,509** | **$40,210** |

**Year 0 (one-time): $79,100.** The capstone build ($3,600 = 24 h × $150), productionization ($72,000: real Kubernetes/cloud adapters instead of the simulator, OIDC, mesh mTLS, a pen test), landing zone ($2,000) and policy review ($1,500). The blueprint's figure was $93,500, with a $18,000 build line for 10 engineers.

## Benefits (SRE capacity freed, valued at $150/h, not headcount reduction)

| | Y1 | Y2 | Y3 |
|---|---|---|---|
| Incidents through the platform / month | 140 | 234 | 321 |
| Outcome mix automated / human-approved / escalated | 40 / 40 / 20 % | 50 / 35 / 15 % | 55 / 32 / 13 % |
| Minutes saved per incident (automated / HITL / escalated) | 38 / 25 / 10 | 38 / 25 / 10 | 38 / 25 / 10 |
| Bad-remediation allowance (2 %, 2 %, 1.5 % × 120 min) | −269 min | −477 min | −503 min |
| **Annual value** | **$106,176** | **$191,014** | **$275,831** |

Downtime and revenue avoided are excluded, which makes this conservative.

## Cash flow

| Year | Cost | Benefit | Net | Cumulative |
|---|---|---|---|---|
| Y0 | $79,100 | — | −$79,100 | −$79,100 |
| Y1 | $45,623 | $106,176 | +$60,553 | −$18,547 |
| Y2 | $46,509 | $191,014 | +$144,505 | +$125,958 |
| Y3 | $40,210 | $275,831 | +$235,622 | **+$361,580** |

## Sensitivity (3-year net)

| Case | 3-year net | Change |
|---|---|---|
| Base | $361,580 | — |
| Lower automation (15 points shifted from automated to human-approved) | $320,917 | −$40,663 |
| Slower adoption (50 / 70 / 75 %) | $230,727 | −$130,853 |
| Lower volume (−30 % incidents) | $189,859 | −$171,721 |
| LLM prices ×2 | $357,363 | −$4,218 |
| Productionization +50 % | $325,580 | −$36,000 |
| Azure warm DR (+$500 / month) | $343,580 | −$18,000 |

Every case stays strongly positive. Doubling LLM prices moves the result by about 1 %, while adoption and volume move it by 36–47 %.

**KPI for operations:** LLM cost per SRE-minute saved on the automated path = $0.0645 / 38 min ≈ **$0.0017**.
