module tb_top;
    reg clk = 1'b0;
    reg rst_n = 1'b1;
    reg enable = 1'b0;
    wire [7:0] count;

    top dut (
        .clk(clk),
        .rst_n(rst_n),
        .enable(enable),
        .count(count)
    );

    always #2.5 clk = ~clk;

    initial begin
        #1000;
        $fatal(1, "watchdog timeout");
    end

    initial begin
        #1 rst_n = 1'b0;
        @(posedge clk);
        #1;
        if (count !== 8'd0) $fatal(1, "reset did not clear count");

        rst_n = 1'b1;
        enable = 1'b1;
        @(posedge clk);
        #1;
        if (count !== 8'd1) $fatal(1, "first increment failed");

        @(posedge clk);
        #1;
        if (count !== 8'd2) $fatal(1, "second increment failed");

        enable = 1'b0;
        @(posedge clk);
        #1;
        if (count !== 8'd2) $fatal(1, "disabled counter did not hold");

        $display("RTL_AGENT_TEST_PASS");
        $finish;
    end
endmodule
