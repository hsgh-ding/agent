## Inspect the candidate against the task and report concrete checks

{{NOTICE}}

```verilog
{{CODE}}
```

This is a review-report stage. Return only JSON with 1..6 short checks:
{"checks":[{"requirement":"verbatim task/interface quote","source":"verbatim current RTL fragment","case":"concrete input values or a short input/cycle sequence","expected":"outcome required by the task","observed":"outcome computed from this RTL","status":"match|mismatch|uncertain"}]}

Derive the expected outcome from the task, separately from computing the RTL's
outcome. Report concise results, not internal deliberations. Do not use comments,
successful compilation, or successful synthesis as evidence of correct behavior.
Choose the checks that distinguish a correct implementation from plausible bugs:
- Combinational logic: cover a normal case and relevant zero, maximum, signed,
  carry, priority or width boundary; account for truncation and extension.
- Clocked logic: cover the specified reset, a normal transition and the first
  output cycle; evaluate nonblocking right-hand sides using the OLD state.
  Inspect enable/load priority, pulse clearing and overlap only when relevant.
- Cover distinct requested behaviors, not six restatements of one easy case.

Use identical concise expected/observed values for match, different comparable
values for mismatch. Mark uncertain if the task leaves an outcome unspecified
or you cannot determine it. Do not invent requirements or a clock/reset/latency.
Every requirement and source quote must occur in the supplied task/interface
and current RTL. References are reminders, not additional task requirements.

Do not return KEEP, edits, rewritten RTL, executable commands or a testbench.
The caller checks report anchors, records these as MODEL CLAIMS (not simulation
results), and requests a separate targeted repair only for reported mismatches.
