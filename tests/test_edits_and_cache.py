"""Framework contracts only: no benchmark solutions or model-specific tuning."""
import json
import unittest

from agent.edits import decode_edit_reply, EditError
from agent.verification import VerificationCache
from test_review_pipeline import run_script, PassingTools, WIRE


class EditContractTests(unittest.TestCase):
    def test_unique_edits_apply_in_order_without_mutating_original(self):
        source = "header\nleft = old;\nright = old;\nfooter"
        reply = json.dumps({"edits": [
            {"search": "left = old;", "replace": "left = intermediate;"},
            {"search": "left = intermediate;", "replace": "left = final;"},
        ]})
        result, mode, count = decode_edit_reply(reply, source)
        self.assertEqual(result, "header\nleft = final;\nright = old;\nfooter")
        self.assertEqual((mode, count), ("edits", 2))
        self.assertIn("left = old;", source)

    def test_ambiguous_missing_empty_or_invalid_anchor_is_rejected(self):
        for edit in (
            {"search": "old", "replace": "new"},
            {"search": "missing", "replace": "new"},
            {"search": "", "replace": "new"},
            {"search": "left", "replace": None},
            {"path": "other.v", "search": "left", "replace": "new"},
        ):
            with self.subTest(edit=edit), self.assertRaises(EditError):
                decode_edit_reply(json.dumps({"edits": [edit]}), "left old right old")

    def test_later_invalid_edit_cannot_partially_change_source(self):
        original = "unique original"
        reply = json.dumps({"edits": [
            {"search": "original", "replace": "changed"},
            {"search": "not present", "replace": "other"},
        ]})
        with self.assertRaises(EditError):
            decode_edit_reply(reply, original)
        self.assertEqual(original, "unique original")

    def test_keep_is_accepted_only_for_a_checked_review_candidate(self):
        reply = '{"decision":"keep"}'
        self.assertEqual(decode_edit_reply(reply, WIRE, allow_keep=True), (WIRE, "keep", 0))
        for source in ("", WIRE):
            with self.assertRaises(EditError):
                decode_edit_reply(reply, source)

    def test_json_fence_and_regular_full_rtl_are_supported(self):
        reply = '```json\n{"decision":"keep"}\n```'
        self.assertEqual(decode_edit_reply(reply, WIRE, allow_keep=True)[0], WIRE)
        self.assertIsNone(decode_edit_reply("```verilog\n" + WIRE + "```", WIRE))
        self.assertIsNone(decode_edit_reply(WIRE, WIRE))

    def test_malformed_json_or_executable_fields_are_rejected(self):
        for reply in ('{"edits":', '{"command":"do something"}',
                      '{"decision":"keep","edits":[]}', '{"edits":[]}'):
            with self.subTest(reply=reply), self.assertRaises(EditError):
                decode_edit_reply(reply, WIRE, allow_keep=True)

    def test_patch_candidate_is_checked_before_becoming_final_solution(self):
        wrong = WIRE.replace("y = a", "y = ~a")
        edit = json.dumps({"reason": "correct the requested behavior", "edits": [
            {"search": "y = ~a", "replace": "y = a"}]})
        result, _, tools, events, saved = run_script([wrong, edit])
        self.assertEqual(result, WIRE)
        self.assertEqual(tools.linted, [wrong, WIRE])
        self.assertEqual(tools.synthesized, [wrong, WIRE])
        self.assertEqual(saved[-1], WIRE)
        self.assertTrue(any(e.get("mode") == "edits" for e in events))

    def test_keep_avoids_repeating_source_and_backend_checks(self):
        result, llm, tools, events, _ = run_script([WIRE, '{"decision":"keep"}'])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(len(tools.linted), 1)
        self.assertEqual(len(tools.synthesized), 1)
        self.assertEqual(events[-1]["review"], "unchanged")

    def test_bad_patch_gets_focused_feedback_and_preserves_checked_candidate(self):
        bad = '{"edits":[{"search":"not found","replace":"new"}]}'
        result, llm, tools, events, saved = run_script([WIRE, bad, '{"decision":"keep"}'])
        self.assertEqual(result, WIRE)
        self.assertEqual(saved[-1], WIRE)
        self.assertEqual(len(tools.synthesized), 1)
        self.assertIn("unique exact anchor", llm.requests[2][-1]["content"])
        self.assertTrue(any(e.get("tool") == "edit_validation" and e.get("rc") == 1 for e in events))


class VerificationCacheTests(unittest.TestCase):
    def test_identical_source_top_and_stage_reuse_the_result(self):
        tools = PassingTools()
        cache = VerificationCache(tools)
        self.assertFalse(cache.check("lint", WIRE, "TopModule", 5)[2])
        self.assertTrue(cache.check("lint", WIRE, "TopModule", 5)[2])
        self.assertFalse(cache.check("synth", WIRE, "TopModule", 5)[2])
        self.assertEqual(len(tools.linted), 1)
        self.assertEqual(len(tools.synthesized), 1)
        self.assertFalse(cache.check("lint", WIRE + "\n", "TopModule", 5)[2])
        self.assertFalse(cache.check("lint", WIRE, "OtherTop", 5)[2])

    def test_reverting_to_previous_source_avoids_redundant_backend_calls(self):
        changed = WIRE.replace("y = a", "y = ~a")

        class RejectChanged(PassingTools):
            def synth(self, source, top, timeout_s):
                self.synthesized.append(source)
                return (0, "Synthesis finished") if source == WIRE else (1, "ERROR: unsupported construct")

        result, _, tools, events, _ = run_script([WIRE, changed, WIRE], tools=RejectChanged())
        self.assertEqual(result, WIRE)
        self.assertEqual(tools.linted, [WIRE, changed])
        self.assertEqual(tools.synthesized, [WIRE, changed])
        self.assertEqual(sum(e.get("tool") == "verification_cache" for e in events), 2)

    def test_unavailable_and_transient_results_are_not_cached(self):
        for failure in ((-1, "unavailable"), (1, "tool timed out"), (1, "license server unavailable")):
            class TransientTools(PassingTools):
                def lint(self, source, top, timeout_s):
                    self.linted.append(source)
                    return failure
            with self.subTest(failure=failure):
                tools = TransientTools()
                cache = VerificationCache(tools)
                self.assertFalse(cache.check("lint", WIRE, "TopModule", 5)[2])
                self.assertFalse(cache.check("lint", WIRE, "TopModule", 5)[2])
                self.assertEqual(len(tools.linted), 2)


if __name__ == "__main__":
    unittest.main()
