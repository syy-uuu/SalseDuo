-- 依据 documents_generated/AW_Corporate_Credit_and_Payment_Terms_Policy.docx
-- (Policy ID: AW-FIN-POL-003) 第2节客户分级矩阵 + 第3节例外审批流程实现。
--
-- 设计说明（Tier 1 为何不做成自动判定）：
-- 文档中 Tier 1 Strategic Partner 的资质条件是"Global distributors or strategic OEMs
-- with specific board approval"——这是一个定性的董事会审批门槛，文档并未给出可计算的
-- 数值边界，因此不应臆造一个数字阈值来自动把客户判给 Tier 1。函数改为显式接收
-- board_approved_strategic_partner 参数，由调用方（agent 结合文档/对话上下文确认后）
-- 显式传入 TRUE，否则一律按 New/Tier 3/Tier 2 的可计算数值边界判定。
CREATE OR REPLACE FUNCTION {catalog}.{schema}.calculate_credit_terms(
  relationship_years DOUBLE COMMENT '客户与 Adventure Works 的存续合作年限',
  annual_purchase_volume_usd DOUBLE COMMENT '客户年采购金额（美元）',
  board_approved_strategic_partner BOOLEAN DEFAULT FALSE COMMENT '是否已获得董事会批准为 Tier 1 战略合作伙伴（定性判断，需调用方显式确认，不由本函数自动推断）',
  requested_credit_amount_usd DOUBLE DEFAULT 0 COMMENT '本次申请/占用的信用额度（美元），用于比对该档位允许的最高信用额度',
  requested_term_days INT DEFAULT NULL COMMENT '本次申请的账期天数，用于识别 Net 90 长账期例外'
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
COMMENT 'AW-FIN-POL-003《公司信用额度与付款条款政策》第2/3节：按客户分级矩阵计算信用条款，并判断超限时所需的审批级别。'
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
        -- Tier 1 上不封顶（文档写作 "Above $750,000 USD"），用 NULL 代表无硬性上限，
        -- 但仍需 CFO 签字，由 required_approval 单独体现，不代表可无审批放款。
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
