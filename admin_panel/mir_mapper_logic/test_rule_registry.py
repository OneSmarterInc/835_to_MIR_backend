import unittest
from decimal import Decimal

from .models import Adjustment, Claim, ServiceLine
from .rule_registry import (
    RULE_REGISTRY,
    RuleSeverity,
    claim_control_number,
    evaluate_preventive_rules,
    is_blocking,
)


def adjustment(group, reason, amount):
    return Adjustment(group=group, reason=reason, amount=Decimal(str(amount)))


def codes(findings):
    return {finding["rule_code"] for finding in findings}


class PreventiveRuleRegistryTests(unittest.TestCase):
    def test_registry_has_required_rules_and_severity_vocabulary(self):
        definitions = {rule.code: rule for rule in RULE_REGISTRY.definitions()}
        self.assertTrue({"MP003", "MP011", "MP013", "DUPLICATE_ICN"}.issubset(definitions))
        self.assertEqual(definitions["MP003"].severity, RuleSeverity.REFUSE)
        self.assertEqual(definitions["MP011"].severity, RuleSeverity.REFUSE)
        self.assertEqual(definitions["MP013"].severity, RuleSeverity.REFUSE)
        self.assertEqual(definitions["DUPLICATE_ICN"].severity, RuleSeverity.WARN)
        self.assertEqual({severity.value for severity in RuleSeverity}, {"REFUSE", "HOLD", "WARN"})

    def test_findings_include_rule_provenance(self):
        claim = Claim(claim_number="ABC", claim_reference="123", group_number="")
        finding = next(
            finding for finding in evaluate_preventive_rules(claim)
            if finding["rule_code"] == "MP013"
        )
        self.assertEqual(finding["severity"], "REFUSE")
        self.assertEqual(finding["claim_control_number"], "ABC123")
        self.assertEqual(finding["provenance"]["rule_code"], "MP013")
        self.assertIn("MPL_Exception-Codes-List", finding["source"])

    def test_mp003_cross_foot_passes_when_allowance_equals_pay_plus_liability(self):
        service = ServiceLine(
            charge=Decimal("100"),
            paid=Decimal("70"),
            adjustments=[adjustment("CO", "45", "20"), adjustment("PR", "1", "10")],
        )
        claim = Claim(status="1", group_number="G1", services=[service])
        self.assertNotIn("MP003", codes(evaluate_preventive_rules(claim)))

    def test_mp011_refuses_improper_timely_filing_claim(self):
        service = ServiceLine(
            charge=Decimal("100"),
            paid=Decimal("10"),
            adjustments=[adjustment("CO", "29", "90")],
        )
        claim = Claim(status="1", group_number="G1", services=[service])
        findings = evaluate_preventive_rules(claim)
        self.assertIn("MP011", codes(findings))
        finding = next(finding for finding in findings if finding["rule_code"] == "MP011")
        self.assertEqual(finding["severity"], "REFUSE")
        self.assertEqual(finding["evidence"]["violating_lines"][0]["fund_amount"], "10")

    def test_mp013_refuses_missing_group_number(self):
        claim = Claim(claim_number="A", group_number="")
        self.assertIn("MP013", codes(evaluate_preventive_rules(claim)))

    def test_mp013_respects_pr31_exception(self):
        service = ServiceLine(adjustments=[adjustment("PR", "31", "100")])
        claim = Claim(claim_number="A", group_number="", services=[service])
        self.assertNotIn("MP013", codes(evaluate_preventive_rules(claim)))

    def test_duplicate_icn_uses_full_clp01_plus_clp07(self):
        claim = Claim(
            claim_number="08020260470268000",
            claim_reference="QZL520",
            group_number="G1",
        )
        icn = "08020260470268000QZL520"
        self.assertEqual(claim_control_number(claim), icn)
        findings = evaluate_preventive_rules(claim, seen_icns={icn})
        duplicate = next(finding for finding in findings if finding["rule_code"] == "DUPLICATE_ICN")
        self.assertEqual(duplicate["severity"], "WARN")
        self.assertFalse(is_blocking(duplicate))
        self.assertEqual(duplicate["evidence"]["icn"], icn)
        self.assertIn("current batch", duplicate["evidence"]["duplicate_sources"])

    def test_duplicate_icn_detects_persisted_history(self):
        claim = Claim(claim_number="C1", claim_reference="REF", group_number="G1")
        findings = evaluate_preventive_rules(claim, existing_icns={"C1REF"})
        duplicate = next(finding for finding in findings if finding["rule_code"] == "DUPLICATE_ICN")
        self.assertIn("persisted MIR history", duplicate["evidence"]["duplicate_sources"])


if __name__ == "__main__":
    unittest.main()
