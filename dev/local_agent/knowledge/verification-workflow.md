# Model-invoked generic RTL verification tools

The chat agent uses staged model-selected tools. Saving code never starts
Vivado by itself. Save RTL first, generate a self-testbench when none is
supplied, then call `run_verification_workflow` for the complete sequence.
These tools accept the current saved RTL files for any supported design; there
are no counter, adder, or GCD templates. After successful checks, call
`finish_design` to export the verified revision.

1. Save a complete RTL revision on disk. Never patch only an in-memory answer.
2. Compile and elaborate with Vivado. A compile failure blocks all later stages.
3. Run the frozen testbench. A zero exit code is not enough; require the exact
   `RTL_AGENT_TEST_PASS` marker and reject fatal/assertion failures.
4. Run synthesis only after simulation passes. Preserve reports and diagnostics.
5. If a stage fails, the session enters repair. Inspect the recorded file with
   the report/source tools, create a new revision, then restart validation from
   compile.

The source files and logs on disk are authoritative. Conversation history is
only a summary and must not be treated as the project copy.
