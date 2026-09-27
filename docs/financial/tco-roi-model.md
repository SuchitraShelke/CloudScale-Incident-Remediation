# 3-year TCO / ROI and token economics

**Rubric:** Engineering Package (quantified 3-year TCO and ROI with token optimization strategies)

The live model is [`tco-roi-model.xlsx`](tco-roi-model.xlsx): every figure below is a formula over one Inputs sheet and the Token Economics sheet. Blue cells are inputs you can change. `scripts/tco_model.py` builds it and cross-checks its formulas against an independent Python computation (0 formula errors, 11/11 checkpoints equal).

Inputs follow the blueprint (`CapstoneProjectPlan_v2.md` §12, §17) with two corrections:
1. **Year-0 build:** the actual solo build (about 24 h of one developer working with Claude) instead of 10 engineers × 12 h.
2. **LLM cost per incident:** **measured**, not modeled. It comes from the token ledger over 41 live incidents on OpenAI (`gpt-5.4` / `gpt-5.4-mini`) during the capstone rehearsals, at OpenAI's published standard prices.

Cloud prices are on-demand us-east-1 estimates for planning; validate them with the AWS Pricing Calculator before using them externally.

## Headline

| KPI | Value |
|---|---|
| **3-year net benefit** | **$362,101** |
| **NPV at 10 %** | **$272,825** |
| **Payback** | **month 14** of operation |
| Benefit-cost ratio | 2.72× |
| Net ROI | 172 % |
| SRE hours freed | 708 h in Y1 → 1,839 h in Y3 |
| LLM share of 3-year cost | 1.7 % |
| **LLM cost per incident (as built, measured)** | **$0.0101** |

## Token economics: measured on OpenAI

From the token ledger (`GET /ledger/summary`), live rehearsals on 2026-09-27. Prices per 1M tokens: `gpt-5.4` $2.50 input / $0.25 cached / $15.00 output; `gpt-5.4-mini` $0.75 / $0.075 / $4.50.

| Measured | Value | n |
|---|---|---|
| P1 / tier-1 incident (`gpt-5.4` triage + planner, `mini` summary) | $0.0227 average ($0.016–$0.043) | 21 |
| P2 incident (all `gpt-5.4-mini`) | $0.0055 average ($0.004–$0.010) | 7 |
| Semantic-cache hit (summary call only) | $0.0008 | 13 |
| Input tokens served from OpenAI's prompt cache | 76 % (hits: 90 %) | — |
| Provider failovers | 0 | — |

| Strategy | $ per incident | vs all-deep |
|---|---|---|
| All `gpt-5.4` (no routing, no semantic cache) | $0.0256 | — |
| Tiered routing (`gpt-5.4` for P1 / tier-1, `mini` otherwise) | $0.0124 | −52 % |
| **Tiered routing + semantic cache (as built)** | **$0.0101** | **−61 %** |

Blended with 40 % P1 incidents and a 20 % semantic-cache hit rate (the blueprint's target; demo re-runs hit 100 %, which is not a production figure). The all-`gpt-5.4` row uses the measured triage and planner calls on `gpt-5.4`, plus a summary call estimated from measured tokens. Measured costs already include any retries and re-plans that happened, so no overhead multiplier is applied.

**Three cost levers are visible in the data:**
1. **Tiered routing** halves the cost: P2 incidents run on the small model.
2. **The semantic cache** turns a repeat incident into one $0.0008 summary call.
3. **Prompt caching** serves three-quarters of all input tokens at a tenth of the price. It's free on OpenAI, since the stable system prompt comes first and each agent has its own cache key.

**For comparison: Claude, modeled.** No Anthropic key was available, so these are estimates at list prices: Sonnet 5 $2 / $10 and Haiku 4.5 $1 / $5 per 1M tokens, with blueprint token averages and ×1.3 overhead.

| Claude strategy (modeled) | $ per incident |
|---|---|
| All Sonnet 5 | $0.1171 |
| Tiered routing | $0.0783 |
| Tiered routing + semantic cache | $0.0645 |

The blueprint's token estimates were too high: measured input is about 1–2× lower (triage ~3,550 vs 8,192 tokens), and output is 4–10× lower (triage ~215 vs 2,048; planner ~800 vs 3,072).

**What this means for the jury.** At 200–340 incidents a month, LLM spend is **$2–3 a month**, 1.7 % of the platform's cost. The optimizations matter for three other reasons:
1. **Scale:** the savings are linear in volume.
2. **Latency:** a cache hit skips both LLM calls, the slowest steps in a live run.
3. **Safety:** the cache only reuses *verified* fixes and caps confidence at 0.79, so it can't widen autonomy (ADR-003).

The ROI is driven by adoption and the automation mix, not by model prices. That's why the gate policy and confidence calibration matter more than squeezing tokens.

## Costs

| Line ($ / month) | Y1 | Y2 | Y3 |
|---|---|---|---|
| Infrastructure (EKS, 3→5 app nodes, guard node, RDS Multi-AZ, ElastiCache, networking, observability) | $689 | $759 | $829 |
| LLM production (incidents × $0.0101, measured) | $2 | $3 | $3 |
| LLM evaluation + staging | $100 | $100 | $100 |
| Platform maintenance (20 / 20 / 16 h × $150) | $3,000 | $3,000 | $2,400 |
| **Total per year** | **$45,492** | **$46,339** | **$39,989** |

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
| Y1 | $45,492 | $106,176 | +$60,684 | −$18,416 |
| Y2 | $46,339 | $191,014 | +$144,675 | +$126,259 |
| Y3 | $39,989 | $275,831 | +$235,842 | **+$362,101** |

## Sensitivity (3-year net)

| Case | 3-year net | Change |
|---|---|---|
| Base | $362,101 | — |
| Lower automation (15 points shifted from automated to human-approved) | $321,438 | −$40,663 |
| Slower adoption (50 / 70 / 75 %) | $231,248 | −$130,853 |
| Lower volume (−30 % incidents) | $190,224 | −$171,877 |
| LLM prices ×2 | $358,405 | −$3,696 |
| Productionization +50 % | $326,101 | −$36,000 |
| Azure warm DR (+$500 / month) | $344,101 | −$18,000 |

Every case stays strongly positive. Doubling LLM prices moves the result by about 1 %, while adoption and volume move it by 36–47 %.

**KPI for operations:** measured LLM cost per SRE-minute saved on the automated path = $0.0101 / 38 min ≈ **$0.0003**.
