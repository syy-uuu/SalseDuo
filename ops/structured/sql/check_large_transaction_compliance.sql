-- 依据 documents_generated/AW_Large_Transaction_and_Special_Settlement_Compliance_Regulation-v2.docx
-- (Regulation ID: AW-COMP-REG-014) 第2/3节结算方式与外汇合规规则实现。
CREATE OR REPLACE FUNCTION {catalog}.{schema}.check_large_transaction_compliance(
  settlement_method STRING COMMENT '结算方式，取值之一: WIRE_TRANSFER, LETTER_OF_CREDIT, BANK_ACCEPTANCE_DRAFT, CORPORATE_CHEQUE',
  transaction_amount_usd DOUBLE COMMENT '交易金额（美元）',
  settlement_currency STRING DEFAULT 'USD' COMMENT '结算货币 ISO 代码，如 USD/EUR/AUD/GBP/CAD',
  contract_duration_years DOUBLE DEFAULT 0 COMMENT '合同期限（年），单笔交易传 0；用于判断多年期外汇对冲条款是否强制要求'
)
RETURNS STRUCT<
  compliance_status: STRING,
  settlement_allowed: BOOLEAN,
  required_controls: STRING,
  currency_approved: BOOLEAN,
  fx_hedging_clause_required: BOOLEAN
>
COMMENT 'AW-COMP-REG-014《大额交易与特殊结算合规规定》第2/3节：校验结算方式合规状态及外汇对冲条款是否强制要求。'
RETURN (
  WITH method_calc AS (
    SELECT
      CASE upper(settlement_method)
        WHEN 'WIRE_TRANSFER' THEN 'Standard / Highly Approved'
        WHEN 'LETTER_OF_CREDIT' THEN
          CASE WHEN transaction_amount_usd > 250000 THEN 'Approved for Global Orders > $250k'
               ELSE 'Not Standard Below $250k Threshold' END
        WHEN 'BANK_ACCEPTANCE_DRAFT' THEN 'Restricted / Conditional Approval'
        WHEN 'CORPORATE_CHEQUE' THEN 'Strictly Prohibited for Bulk Supply'
        ELSE 'Unknown Settlement Method'
      END AS compliance_status,
      CASE upper(settlement_method)
        WHEN 'WIRE_TRANSFER' THEN 'Funds must originate from a verified corporate bank account under the identical legal entity name matching the sales contract. No third-party payments allowed.'
        WHEN 'LETTER_OF_CREDIT' THEN 'Must be issued as an Irrevocable Letter of Credit confirmed by a top-tier international financial institution. Subject to pre-clearance by Treasury Operations.'
        WHEN 'BANK_ACCEPTANCE_DRAFT' THEN 'Only accepted within specific domestic jurisdictions from AAA-rated banks. Must be verified and cleared by Corporate Treasury before order release.'
        WHEN 'CORPORATE_CHEQUE' THEN 'Not accepted for large transactions due to severe clearing delays and bounce risks. Banned for global distributors without a valid corporate waiver document.'
        ELSE 'No matching control found for the given settlement method.'
      END AS required_controls,
      CASE upper(settlement_method)
        WHEN 'CORPORATE_CHEQUE' THEN FALSE
        WHEN 'LETTER_OF_CREDIT' THEN transaction_amount_usd > 250000
        ELSE TRUE
      END AS settlement_allowed
  )
  SELECT STRUCT(
    compliance_status,
    settlement_allowed,
    required_controls,
    upper(settlement_currency) IN ('USD', 'EUR', 'AUD', 'GBP', 'CAD') AS currency_approved,
    (upper(settlement_currency) != 'USD' AND contract_duration_years > 1 AND transaction_amount_usd > 1000000) AS fx_hedging_clause_required
  )
  FROM method_calc
);
