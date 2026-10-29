"""Offline regression tests for routing, source hints and bounded review.

Scripted model replies test orchestration, not Qwen's ability or contest scores.
"""
from pathlib import Path
import ast
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest import mock

from agent.advisory import rtl_review_hints
from agent.main import KNOWLEDGE_DIR, solve_agent
from agent.retrieval import retrieve
from agent.skills import Skill, load_skills, select
from agent.tools import RtlToolchain, summarize_log

ROOT = Path(__file__).resolve().parents[1]
WIRE = "module TopModule(input a, output y); assign y = a; endmodule\n"


class ScriptedLLM:
    timeout_s = 600

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages):
        self.requests.append(messages)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text="```verilog\n" + reply + "\n```",
                               tokens_in=10, tokens_out=20)


class Events:
    def __init__(self):
        self.items = []

    def write(self, **fields):
        self.items.append(fields)


class PassingTools:
    available = True
    reason = ""

    def __init__(self):
        self.linted, self.synthesized = [], []

    def lint(self, source, top, timeout_s):
        self.linted.append(source)
        return 0, ""

    def synth(self, source, top, timeout_s):
        self.synthesized.append(source)
        return 0, "Synthesis finished with 0 errors, 0 critical warnings and 0 warnings."


def run_script(replies, *, tools=None, prompt="Implement a combinational buffer.",
               interface="module TopModule(input a, output y);", rounds=4, review=True,
               review_mode="legacy"):
    llm, events, saved = ScriptedLLM(replies), Events(), []
    tools = tools or PassingTools()
    with mock.patch("agent.main.RtlToolchain", return_value=tools), \
         mock.patch("agent.main.MAX_ROUNDS", rounds), \
         mock.patch("agent.main.BEHAVIOR_REVIEW", review), \
         mock.patch("agent.main.REVIEW_MODE", review_mode):
        result = solve_agent(llm, events, prompt, interface, "TopModule", save=saved.append)
    return result, llm, tools, events.items, saved


class ReviewPipelineTests(unittest.TestCase):
    def test_synthesis_success_still_gets_one_behavior_review(self):
        result, llm, tools, events, _ = run_script([WIRE, WIRE])
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(len(tools.synthesized), 1)  # unchanged RTL needs no rerun
        self.assertEqual(len(tools.linted), 1)
        self.assertEqual(sum(e.get("event") == "request_behavior_review" for e in events), 1)
        self.assertTrue(any(e.get("review") == "unchanged" for e in events))
        self.assertFalse(any(e.get("event") == "stalled_candidate" for e in events))
        self.assertIn("concrete failing input", llm.requests[1][-1]["content"])
        self.assertFalse(events[-1]["functional_verified"])

    def test_first_generation_is_task_only_and_review_gets_references(self):
        _, llm, _, events, _ = run_script([WIRE, WIRE], prompt="Implement a clocked register with reset.")
        self.assertEqual(len(llm.requests[0]), 2)
        self.assertIn("no markdown fences", llm.requests[0][0]["content"])
        self.assertNotIn("<reference", llm.requests[0][-1]["content"])
        self.assertIn("<reference", llm.requests[1][-1]["content"])
        retrieval = [e for e in events if e.get("tool") == "knowledge"]
        self.assertEqual(retrieval[0]["selected"], [])

    def test_corrected_review_candidate_is_rechecked_and_no_review_loop(self):
        wrong = WIRE.replace("y = a", "y = ~a")
        result, llm, tools, events, saved = run_script([wrong, WIRE])
        self.assertEqual(result, WIRE)
        self.assertEqual(tools.linted, [wrong, WIRE])
        self.assertEqual(tools.synthesized, [wrong, WIRE])
        self.assertEqual(len(llm.requests), 2)
        self.assertEqual(saved[-1], WIRE)
        self.assertEqual(events[-1]["review"], "completed")

    def test_invalid_review_does_not_replace_synthesized_candidate(self):
        invalid = WIRE.replace("TopModule", "WrongModule")
        result, _, tools, _, saved = run_script([WIRE, invalid], rounds=2)
        self.assertEqual(result, WIRE)
        self.assertEqual(saved[-1], WIRE)
        self.assertEqual(len(tools.synthesized), 1)

    def test_failed_review_repair_keeps_compilable_candidate(self):
        changed = WIRE.replace("y = a", "y = ~a")

        class RejectSecond(PassingTools):
            def lint(self, source, top, timeout_s):
                self.linted.append(source)
                return (0, "") if source == WIRE else (1, "ERROR: syntax error")

        result, llm, _, _, saved = run_script([WIRE, changed, changed], tools=RejectSecond())
        self.assertEqual(result, WIRE)
        self.assertEqual(saved[-1], WIRE)
        self.assertEqual(len(llm.requests), 3)

    def test_tool_disappearing_after_review_keeps_checked_candidate(self):
        changed = WIRE.replace("y = a", "y = ~a")

        class MissingAfterFirst(PassingTools):
            def synth(self, source, top, timeout_s):
                self.synthesized.append(source)
                return (0, "Synthesis finished") if source == WIRE else (-1, "unavailable")

        result, _, _, _, saved = run_script([WIRE, changed], tools=MissingAfterFirst())
        self.assertEqual(result, WIRE)
        self.assertEqual(saved[-1], WIRE)

    def test_model_failure_during_review_keeps_saved_candidate(self):
        from agent.llm import LLMError
        result, _, _, _, saved = run_script([WIRE, LLMError("test timeout")])
        self.assertEqual(result, WIRE)
        self.assertEqual(saved[-1], WIRE)

    def test_no_toolchain_is_not_reported_as_verified(self):
        class MissingTools(PassingTools):
            available = False
            reason = "test environment has no Vivado"

            def lint(self, *args, **kwargs):
                return -1, self.reason

        result, llm, _, events, _ = run_script([WIRE], tools=MissingTools())
        self.assertEqual(result, WIRE)
        self.assertEqual(len(llm.requests), 1)
        self.assertFalse(any(e.get("event") == "accept" for e in events))

    def test_review_switch_and_round_limit_bound_model_calls(self):
        for kwargs in ({"review": False}, {"rounds": 1}):
            with self.subTest(kwargs=kwargs):
                _, llm, _, events, _ = run_script([WIRE], **kwargs)
                self.assertEqual(len(llm.requests), 1)
                self.assertEqual(events[-1]["review"], "skipped")

    def test_synth_warning_and_width_hint_reach_review(self):
        # Generic shift register, no task ID, fixed answer or reference RTL.
        bad = ("module TopModule(input clk, input bit_in, output reg [5:0] q);\n"
               "reg [2:0] tap; always @(posedge clk) begin\n"
               "tap = bit_in; q <= {q[4:0], tap}; end endmodule\n")
        good = bad.replace("reg [2:0] tap", "reg tap")

        class WarningTools(PassingTools):
            def synth(self, source, top, timeout_s):
                self.synthesized.append(source)
                return 0, "WARNING: [Synth 8-TEST] width truncation\nSynthesis finished"

        result, llm, tools, events, _ = run_script(
            [bad, good], tools=WarningTools(), prompt="Shift in the serial input each cycle.",
            interface="module TopModule(input clk, input bit_in, output reg [5:0] q);",
        )
        self.assertEqual(result, good)
        self.assertEqual(len(tools.synthesized), 2)
        review = llm.requests[1][-1]["content"]
        self.assertIn("width 8 assigned to 6 bits", review)
        self.assertIn("[Synth 8-TEST]", review)
        self.assertTrue(any(e.get("hints") for e in events))


class RoutingAndDiagnosticsTests(unittest.TestCase):
    def test_seed_matches_official_rtl_baseline_prompt_without_modifying_baseline(self):
        tree = ast.parse((ROOT / "baseline.py").read_text(encoding="utf-8"))
        baseline_prompts = next(ast.literal_eval(node.value) for node in tree.body
                                if isinstance(node, ast.Assign)
                                and any(isinstance(t, ast.Name) and t.id == "SYS" for t in node.targets))
        seed = (ROOT / "agent/prompts/seed.md").read_text(encoding="utf-8").strip()
        self.assertEqual(seed.replace("{{TOP}}", "TopModule"), baseline_prompts["rtl"])

    def test_unrelated_notes_do_not_match_common_interface_prose(self):
        query = "Implement a module named TopModule with the following interface. All input and output ports are one bit."
        self.assertEqual(retrieve(query, KNOWLEDGE_DIR), [])
        for query in ("arithmetic unsigned width input output", "register reset signed width"):
            ids = {n["id"] for n in retrieve(query, KNOWLEDGE_DIR)}
            self.assertIn("arithmetic-width", ids)
            self.assertNotIn("sequence-detector", ids)

    def test_skill_selection_ignores_generic_port_words_and_substrings(self):
        skills = load_skills(str(ROOT / "skill"))
        for query in ("module TopModule input output bits arithmetic width",
                      "module TopModule input output bits sequence detector overlap"):
            names = {s.name for s in select(skills, query)}
            self.assertIn("rtl-fsm-and-widths", names)
            self.assertNotIn("rtl-module-interface-contract", names)
        names = {s.name for s in select(skills, "showcase restoration")}
        self.assertEqual(names, {"rtl-design-checklist"})

    def test_explicit_no_clock_or_reset_does_not_trigger_sequential_guidance(self):
        query = "Combinational signed arithmetic with explicit width. No clock or reset."
        ids = {n["id"] for n in retrieve(query, KNOWLEDGE_DIR)}
        self.assertIn("arithmetic-width", ids)
        self.assertNotIn("sequential-reset", ids)
        skills = load_skills(str(ROOT / "skill"))
        self.assertNotIn("rtl-reset-and-clock", {s.name for s in select(skills, query)})

    def test_error_skill_still_routes_interface_diagnostics(self):
        skills = load_skills(str(ROOT / "skill"))
        selected = select(skills, "implement a register", "interface mismatch: missing port 'q'")
        self.assertIn("rtl-module-interface-contract", {s.name for s in selected})
        rendered = "\n".join(s.render() for s in selected)
        self.assertNotIn("Rule-by-Rule Evaluation", rendered)
        self.assertNotIn("Functional correctness, reset style", rendered)

    def test_skill_rendering_has_hard_cap_even_for_one_large_section(self):
        skill = Skill("test", "", [], "", [], "## Rules\n" + "- rule\n" * 1000, "")
        self.assertLessEqual(len(skill.render(200)), 260)
        self.assertIn("shortened", skill.render(200))

    def test_simple_concat_truncation_is_advisory_not_automatic_rewrite(self):
        source = ("module D(input clk, input [3:0] fragment, output reg [7:0] q);"
                  "always @(posedge clk) q <= {q[6:0], fragment}; endmodule")
        hints = rtl_review_hints(source)
        self.assertEqual(len(hints), 1)
        self.assertIn("width 11 assigned to 8 bits", hints[0])
        self.assertIn("intentional truncation is legal", hints[0])
        self.assertEqual(rtl_review_hints(source.replace("[3:0] fragment", "fragment")), [])

    def test_task_operation_keeps_priority_over_incidental_log_terms(self):
        task = "arithmetic signed width"
        log = "clock reset clk synchronous register state edge warning"
        notes = retrieve(task + "\n" + log, KNOWLEDGE_DIR, task=task)
        self.assertEqual(notes[0]["id"], "arithmetic-width")

    def test_arithmetic_skill_does_not_inject_sequence_example(self):
        skill = next(s for s in load_skills(str(ROOT / "skill")) if s.name == "rtl-fsm-and-widths")
        self.assertNotIn("1101", skill.render(context="arithmetic width"))
        self.assertIn("1101", skill.render(budget=5000, context="sequence detector"))

    def test_advisory_skips_unknown_or_ambiguous_widths_and_comments(self):
        for source in (
            "module D(input clk, output reg [7:0] q); // q <= {q,q};\nendmodule",
            "module D #(parameter W=8)(input clk, input [W-1:0] x, output reg [7:0] q); always @(posedge clk) q <= {x,x}; endmodule",
            "module D(input clk, output reg [7:0] q); reg [3:0] t; begin reg [4:0] t; q <= {t,t}; end endmodule",
        ):
            with self.subTest(source=source):
                self.assertEqual(rtl_review_hints(source), [])

    def test_shift_bit_instead_of_slice_emits_zero_extension_hint(self):
        source = ("module D(input clk, input serial, output reg [5:0] q);"
                  "always @(posedge clk) q <= {q[5], serial}; endmodule")
        self.assertIn("width 2 assigned to 6 bits zero-extends 4", rtl_review_hints(source)[0])
        self.assertEqual(rtl_review_hints(source.replace("q[5]", "q[4:0]")), [])

    def test_warning_details_survive_and_errors_take_priority(self):
        log = ("INFO: elaborating\nWARNING: [Synth 8-A] multiple drivers on q\n"
               "WARNING: [Synth 8-B] width truncation on x\n"
               "Synthesis finished with 0 errors, 0 critical warnings and 2 warnings.")
        summary = summarize_log(log)
        self.assertIn("multiple drivers on q", summary)
        self.assertIn("width truncation on x", summary)
        self.assertIn("Synthesis finished", summary)
        crowded = "WARNING: irrelevant\n" * 200 + "ERROR: first actionable failure\n" + log
        self.assertTrue(summarize_log(crowded, 150).startswith("ERROR: first actionable failure"))

    def test_lint_uses_one_shared_deadline_for_both_processes(self):
        tool = RtlToolchain()
        tool.xvlog, tool.xelab, tool.vivado, tool.reason = "xvlog", "xelab", "vivado", ""
        with tempfile.TemporaryDirectory() as scratch, \
             mock.patch("agent.tools._scratch_dir", return_value=scratch), \
             mock.patch("agent.tools._run", return_value=(0, "ok")) as run, \
             mock.patch("agent.tools.time.monotonic", side_effect=[100, 107]):
            rc, _ = tool.lint(WIRE, "TopModule", timeout_s=10)
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args_list[0].args[-1], 10)
        self.assertEqual(run.call_args_list[1].args[-1], 3)

    def test_synthesis_multi_driver_warning_cannot_pass_with_exit_zero(self):
        tool = RtlToolchain()
        tool.xvlog, tool.xelab, tool.vivado, tool.reason = "xvlog", "xelab", "vivado", ""
        for message, want_rc in (
            ("CRITICAL WARNING: [Synth 8-6859] multi-driven net on q", 1),
            ("WARNING: [Synth 8-TEST] multiple drivers on q", 1),
            ("WARNING: [Synth 8-TEST] intentional latch inferred", 0),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as scratch, \
                 mock.patch("agent.tools._scratch_dir", return_value=scratch), \
                 mock.patch("agent.tools._run", return_value=(0, message + "\nSynthesis finished")):
                rc, log = tool.synth(WIRE, "TopModule")
                self.assertEqual(rc, want_rc)
                self.assertIn(message, log)

    def test_out_of_bounds_select_cannot_pass_lint_with_exit_zero(self):
        tool = RtlToolchain()
        tool.xvlog, tool.xelab, tool.vivado, tool.reason = "xvlog", "xelab", "vivado", ""
        log = "WARNING: [VRFC 10-3705] select index 6 into 'q' is out of bounds"
        with tempfile.TemporaryDirectory() as scratch, \
             mock.patch("agent.tools._scratch_dir", return_value=scratch), \
             mock.patch("agent.tools._run", side_effect=[(0, log), (0, "elaborated")]):
            rc, summary = tool.lint(WIRE, "TopModule")
        self.assertEqual(rc, 1)
        self.assertIn("out of bounds", summary)


class ModelResponseTests(unittest.TestCase):
    def call_with_body(self, body):
        from agent.llm import LLM
        response = SimpleNamespace(status_code=200, json=lambda: body)
        requests_stub = SimpleNamespace(post=mock.Mock(return_value=response),
                                        RequestException=OSError)
        with mock.patch.dict(sys.modules, {"requests": requests_stub}):
            llm = LLM()
            return llm._chat_openai([{"role": "user", "content": "test"}])

    def test_truncated_code_is_rejected_even_if_it_contains_endmodule(self):
        from agent.llm import LLMError
        with self.assertRaisesRegex(LLMError, "truncated RTL"):
            self.call_with_body({"choices": [{"finish_reason": "length", "message": {"content": WIRE}}]})

    def test_reasoning_only_reply_does_not_replace_a_candidate(self):
        from agent.llm import LLMError
        with self.assertRaisesRegex(LLMError, "no final content"):
            self.call_with_body({"choices": [{"finish_reason": "stop", "message": {"reasoning_content": "test-only placeholder"}}]})

    def test_complete_reply_uses_final_content_and_reports_usage(self):
        result = self.call_with_body({"choices": [{"finish_reason": "stop", "message": {"content": WIRE}}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 20}})
        self.assertEqual(result.text, WIRE)
        self.assertEqual((result.tokens_in, result.tokens_out), (5, 20))


if __name__ == "__main__":
    unittest.main()
