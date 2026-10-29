from .storage import atomic_json, write_text
from .vivado import StageResult, create_tcl, error_fingerprint


COUNTER_RTL = """module top (
    input wire clk,
    input wire rst_n,
    input wire enable,
    output reg [7:0] count
);
always @(posedge clk) begin
    if (!rst_n) count <= 8'd0;
    else if (enable) count <= count + 8'd1;
end
endmodule
"""


class DemoRunner:
    def __init__(self, config):
        self.config = config

    def run(self, stage, attempt_dir, sources, testbench, constraints):
        stage_dir = attempt_dir / "stages" / stage
        stage_dir.mkdir(parents=True)
        script = stage_dir / "run.tcl"
        write_text(script, create_tcl(self.config, stage, attempt_dir, sources, testbench, constraints))
        bad = stage == "compile" and any("count + ;" in p.read_text(encoding="utf-8") for p in sources)
        text = "ERROR: [VRFC demo] syntax error near ';'.\n原始报错演示行，修复前不可截断。\n" if bad else f"DEMO ONLY: {stage} succeeded.\n"
        diagnostics = stage_dir / "diagnostics.txt"
        write_text(diagnostics, text)
        if stage == "synthesis":
            write_text(attempt_dir / "reports" / "demo_notice.txt", "模拟综合结果，不是真实网表。\n")
        summary = f"[模拟工具] {stage}: {'失败' if bad else '通过'}"
        atomic_json(stage_dir / "result.json", {"demo": True, "ok": not bad, "summary": summary})
        return StageResult(stage, not bad, 1 if bad else 0, False, 0.0,
                           diagnostics, script, summary, error_fingerprint(stage, text))
