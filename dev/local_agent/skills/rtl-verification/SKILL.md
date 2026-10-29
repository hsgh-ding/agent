# RTL design and model-selected verification tools

Use this workflow when the user asks to create, change, debug, or verify RTL.

1. Confirm the requested behavior, configured top-level name, ports, clock,
   reset polarity, and whether the testbench is supplied or model-generated.
2. For a small module, submit the complete RTL in one write. For a large module,
   write each complete source file to disk in bounded chunks: `replace` once,
   then `append`; attach the required self-testbench on the first chunk only.
3. Commit the completed draft. Read back relevant saved lines when fixing a
   reported error; the file on disk, not old chat text, is the source of truth.
4. Choose and call `run_verification_workflow` to run Vivado compile/elaborate,
   simulation against the frozen testbench, then synthesis. Writing code never
   invokes this tool automatically. Stop at the first failure and inspect the
   saved report with `read_report` and the RTL with `read_source` before editing.
   After successful checks, call `finish_design` to export.
5. Treat the model's own testbench as a self-check, not independent proof.
   Never alter a frozen testbench, weaken assertions, or claim success without
   the corresponding tool result.

Keep complete source files and logs on disk. Put only the task requirements,
fixed constraints, concise relevant knowledge, and selected source/report lines
into the model request. If fixed evidence does not fit, stop without truncating
it and ask for a smaller task or a larger supported context window.
