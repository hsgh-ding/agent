## Repair only a confirmed mismatch with the task

The current candidate already passed structural checks. The report below is a
reviewer's claim, not a compiler error or an executed functional test. Compare
its expected outcome with the task and its observed outcome with the RTL. Ignore
unsupported claims and unspecified behavior. Do not search for a tool ERROR.

{{LOG}}

## Current candidate

```verilog
{{CODE}}
```

Correct only confirmed defects, preserving the interface, specified timing and
unrelated behavior. Prefer exact-anchor JSON edits:
{"reason":"brief confirmed mismatch","edits":[{"search":"unique current source text","replace":"corrected source text"}]}

If the report's claims are incorrect, return the current RTL unchanged in one
verilog block. A complete corrected RTL block is also accepted. No commands,
paths, additional files, testbenches, or invented clock/reset requirements.
All changes will be rechecked. Do not claim functional validation occurred.
