## Review the existing candidate against the task

{{NOTICE}}

```verilog
{{CODE}}
```

Check actual behavior, not comments or synthesis success:
- Work out concrete input/output examples: zero, a single active input bit,
  maximum values, and any task-specific boundary. Verify the requested operation.
- Calculate each concatenation's width and which bits survive assignment. Check
  arithmetic carry/overflow and bitwise versus arithmetic operations.
- For clocked logic, trace reset and several active edges using the OLD register
  values on the right of nonblocking assignments. Check output latency, pulse
  clearing, enable/load priority, state mismatch transitions and allowed overlap.
- Check that each register has one driver. Treat warnings as evidence to inspect,
  not as automatic proof of a functional error. Intentional truncation is legal.

Change behavior only if you can identify a specific mismatch with the task or
structural fault; first work out a concrete failing input or cycle sequence.
Do not invent requirements when the task is ambiguous. If no defect is found,
return only {"decision":"keep"}. Otherwise prefer a small JSON response:
{"reason":"specific defect or counterexample","edits":[{"search":"exact old text","replace":"corrected text"}]}
Each search must match exactly once; include surrounding lines if necessary.
No filesystem paths or executable commands. The caller applies all edits
atomically and rechecks the result. A complete corrected RTL file in one
```verilog block is accepted as a fallback. Never output a testbench.
