"""Report/repair orchestration contracts; these do not measure model accuracy."""
import json
import unittest

from agent.review import parse_review, ReviewError
from test_review_pipeline import WIRE, PassingTools, run_script


TASK = "Implement a combinational buffer."
WRONG = WIRE.replace("y = a", "y = ~a")


def report(*, wrong=False, status=None):
    return json.dumps({"checks": [{
        "requirement": "combinational buffer",
        "source": "assign y = ~a;" if wrong else "assign y = a;",
        "case": "a=0",
        "expected": "y=0",
        "observed": "y=1" if wrong else "y=0",
        "status": status or ("mismatch" if wrong else "match"),
    }]})


class ReportValidationTests(unittest.TestCase):
    def test_quotes_are_grounded_but_report_does_not_claim_execution(self):
        checks = parse_review(report(), TASK, WIRE)
        self.assertEqual(checks[0]["status"], "match")
        self.assertEqual(len(parse_review("```json\n" + report() + "\n```", TASK, WIRE)), 1)

    def test_bare_keep_unanchored_claims_commands_and_edits_are_rejected(self):
        for reply in ('{"decision":"keep"}', '{"checks":[]}',
                      '{"edits":[{"search":"a","replace":"b"}]}',
                      '{"command":"run"}', WIRE,
                      report().replace("combinational buffer", "active-low reset"),
                      report().replace("assign y = a;", "nonexistent_source")):
            with self.subTest(reply=reply), self.assertRaises(ReviewError):
                parse_review(reply, TASK, WIRE)

    def test_contradictory_or_duplicate_checks_are_rejected(self):
        for reply in (report(status="mismatch"), report(wrong=True, status="match")):
            with self.subTest(reply=reply), self.assertRaisesRegex(ReviewError, "consistent"):
                parse_review(reply, TASK, WRONG if "~a" in reply else WIRE)
        data = json.loads(report())
        data["checks"] *= 2
        with self.assertRaisesRegex(ReviewError, "duplicate"):
            parse_review(json.dumps(data), TASK, WIRE)


class EvidenceReviewPipelineTests(unittest.TestCase):
    def run_evidence(self, replies, **kwargs):
        return run_script(replies, prompt=TASK, review_mode="evidence", **kwargs)

    def test_grounded_match_finishes_without_rechecking_or_rewriting(self):
        result, llm, tools, events, _ = self.run_evidence([WIRE, report()])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(tools.synthesized, [WIRE])
        review = next(e for e in events if e.get("tool") == "spec_review")
        self.assertEqual(review["outcome"], "match_claim")
        self.assertFalse(review["functional_verified"])
        self.assertNotIn("return\n{\"decision\":\"keep\"}", llm.requests[1][0]["content"])

    def test_mismatch_requests_targeted_repair_and_rechecks_changed_source(self):
        patch = json.dumps({"reason": "buffer must preserve the input", "edits": [
            {"search": "assign y = ~a;", "replace": "assign y = a;"}]})
        result, llm, tools, events, _ = self.run_evidence([WRONG, report(wrong=True), patch])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 3)
        self.assertEqual(tools.linted, [WRONG, WIRE])
        self.assertEqual(tools.synthesized, [WRONG, WIRE])
        self.assertTrue(any(e.get("event") == "request_semantic_repair" for e in events))
        request = llm.requests[2][-1]["content"]
        self.assertIn("a=0", request)
        self.assertIn("not a compiler error", request)
        self.assertNotIn("先定位第一条", request)

    def test_invalid_reports_cannot_rewrite_source_and_have_bounded_retry(self):
        result, llm, tools, events, saved = self.run_evidence(
            [WIRE, '{"decision":"keep"}', WRONG])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 3)
        self.assertEqual(tools.synthesized, [WIRE])
        self.assertEqual(saved[-1], WIRE)
        self.assertEqual(events[-1]["review"], "invalid_report_original_retained")

    def test_uncertain_report_does_not_trigger_speculative_repair(self):
        result, llm, _, events, _ = self.run_evidence([WIRE, report(status="uncertain")])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(events[-1]["review"], "inconclusive")

    def test_structurally_failed_repair_retains_original_checked_source(self):
        invalid = WIRE.replace("assign y = a;", "assign y = ;")

        class FailChanged(PassingTools):
            def lint(self, source, top, timeout_s):
                self.linted.append(source)
                return (0, "") if source == WRONG else (1, "ERROR: syntax error")

        result, _, _, _, saved = self.run_evidence(
            [WRONG, report(wrong=True), invalid], tools=FailChanged(), rounds=3)
        self.assertEqual(result, WRONG)
        self.assertEqual(saved[-1], WRONG)

    def test_bad_patch_keeps_semantic_context_for_the_format_retry(self):
        bad = '{"edits":[{"search":"absent anchor","replace":"new"}]}'
        patch = json.dumps({"edits": [
            {"search": "assign y = ~a;", "replace": "assign y = a;"}]})
        result, llm, _, _, _ = self.run_evidence([WRONG, report(wrong=True), bad, patch])
        self.assertEqual(result, WIRE)
        request = llm.requests[3][-1]["content"]
        self.assertIn("a=0", request)
        self.assertIn("not a compiler error", request)
        self.assertNotIn("先定位第一条", request)

    def test_repair_can_reject_a_reviewers_false_claim_without_rewriting(self):
        result, _, tools, events, _ = self.run_evidence([WRONG, report(wrong=True), WRONG])
        self.assertEqual(result, WRONG)
        self.assertEqual(tools.synthesized, [WRONG])
        self.assertEqual(events[-1]["review"], "claim_not_confirmed")

    def test_success_warning_does_not_route_functional_review_to_diagnostic_notes(self):
        class WarningTools(PassingTools):
            def lint(self, source, top, timeout_s):
                self.linted.append(source)
                return 0, "WARNING: xelab environment warning"

        _, _, _, events, _ = self.run_evidence([WIRE, report()], tools=WarningTools())
        retrieval = [e for e in events if e.get("tool") == "knowledge"]
        self.assertNotIn("synthesis-and-errors", retrieval[1]["selected"])
