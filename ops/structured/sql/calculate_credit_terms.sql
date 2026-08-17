-- Implements the customer tiering matrix (section 2) and exception approval workflow
-- (section 3) from documents_generated/AW_Corporate_Credit_and_Payment_Terms_Policy.docx
-- (Policy ID: AW-FIN-POL-003).
--
-- Design note (why Tier 1 isn't auto-determined): the document's qualification
-- criteria for Tier 1 Strategic Partner is "Global distributors or strategic OEMs with
-- specific board approval" — a qualitative board-approval threshold, and the document
-- gives no computable numeric boundary for it, so this function should not invent a
-- numeric threshold to auto-assign a customer to Tier 1. Instead the function takes an
-- explicit board_approved_strategic_partner parameter, which the caller (the agent,
-- after confirming from the document/conversation context) passes as TRUE explicitly;
-- otherwise the customer is always evaluated against the computable numeric boundaries
-- for New/Tier 3/Tier 2.
CREATE OR REPLACE FUNCTION {catalog}.{schema}.calculate_credit_terms(
  relationship_years DOUBLE COMMENT 'Number of years the customer has had an ongoing relationship with Adventure Works',
  annual_purchase_volume_usd DOUBLE COMMENT 'Customer annual purchase volume (USD)',
  board_approved_strategic_partner BOOLEAN DEFAULT FALSE COMMENT 'Whether board approval has been obtained for Tier 1 Strategic Partner status (a qualitative judgment call the caller must confirm explicitly — this function never infers it automatically)',
  requested_credit_amount_usd DOUBLE DEFAULT 0 COMMENT 'The credit amount being requested/utilized in this case (USD), used to check against this tier''s maximum allowed credit limit',
  requested_term_days INT DEFAULT NULL COMMENT 'The requested payment term in days for this case, used to detect the Net 90 long-term-payment exception'
)
RETURNS STRUCT<
  tier: STRING,
  advance_payment_min_pct: DOUBLE,
  advance_payment_max_pct: DOUBLE,
  max_credit_term_days: INT,
  max_credit_limit_usd: DOUBLE,
  exceeds_credit_limit: BOOLEAN,
  overage_pct: DOUBLE,
  requires_net90_escalation: BOOLEAN,
  required_approval: STRING
>
COMMENT 'AW-FIN-POL-003 "Corporate Credit and Payment Terms Policy" sections 2/3: computes credit terms per the customer tiering matrix, and determines the approval level required when a limit is exceeded.'
RETURN (
  WITH tier_calc AS (
    SELECT
      CASE
        WHEN board_approved_strategic_partner THEN 'Tier 1 Strategic Partner'
        WHEN relationship_years >= 3 AND annual_purchase_volume_usd > 1000000 THEN 'Tier 2 Preferred Account'
        WHEN relationship_years >= 1 THEN 'Tier 3 Standard Account'
        ELSE 'New Customer (First 12 Months)'
      END AS tier
  ),
  matrix AS (
    SELECT
      tier,
      CASE tier
        WHEN 'Tier 1 Strategic Partner' THEN 0.0
        WHEN 'Tier 2 Preferred Account' THEN 0.0
        WHEN 'Tier 3 Standard Account' THEN 0.0
        ELSE 0.30
      END AS advance_payment_min_pct,
      CASE tier
        WHEN 'Tier 1 Strategic Partner' THEN 0.0
        WHEN 'Tier 2 Preferred Account' THEN 0.0
        WHEN 'Tier 3 Standard Account' THEN 0.10
        ELSE 0.30
      END AS advance_payment_max_pct,
      CASE tier
        WHEN 'Tier 1 Strategic Partner' THEN 90
        WHEN 'Tier 2 Preferred Account' THEN 60
        WHEN 'Tier 3 Standard Account' THEN 45
        ELSE 30
      END AS max_credit_term_days,
      CASE tier
        -- Tier 1 has no hard cap (the document writes it as "Above $750,000 USD"); NULL
        -- represents "no hard ceiling", but CFO sign-off is still required — captured
        -- separately via required_approval, not implying approval-free lending.
        WHEN 'Tier 1 Strategic Partner' THEN NULL
        WHEN 'Tier 2 Preferred Account' THEN 750000.0
        WHEN 'Tier 3 Standard Account' THEN 250000.0
        ELSE 50000.0
      END AS max_credit_limit_usd
    FROM tier_calc
  ),
  exceed_calc AS (
    SELECT
      m.*,
      CASE
        WHEN m.max_credit_limit_usd IS NULL THEN FALSE
        ELSE requested_credit_amount_usd > m.max_credit_limit_usd
      END AS exceeds_credit_limit,
      CASE
        WHEN m.max_credit_limit_usd IS NULL OR m.max_credit_limit_usd = 0 THEN 0.0
        ELSE GREATEST(0.0, (requested_credit_amount_usd - m.max_credit_limit_usd) / m.max_credit_limit_usd * 100)
      END AS overage_pct,
      (m.tier != 'Tier 1 Strategic Partner' AND requested_term_days IS NOT NULL AND requested_term_days >= 90) AS requires_net90_escalation
    FROM matrix m
  )
  SELECT STRUCT(
    tier,
    advance_payment_min_pct,
    advance_payment_max_pct,
    max_credit_term_days,
    max_credit_limit_usd,
    exceeds_credit_limit,
    overage_pct,
    requires_net90_escalation,
    CASE
      WHEN NOT exceeds_credit_limit AND NOT requires_net90_escalation THEN 'NONE'
      WHEN requires_net90_escalation OR overage_pct > 15 THEN 'VP_SALES_AND_CFO_SIGNOFF'
      WHEN overage_pct > 0 THEN 'REGIONAL_DIRECTOR_AND_RISK_MANAGER_SIGNOFF'
      ELSE 'NONE'
    END AS required_approval
  )
  FROM exceed_calc
);
