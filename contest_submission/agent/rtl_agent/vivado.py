from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from .design import PASS_MARKER
from .errors import AgentError
from .storage import atomic_json, read_text, write_text


STAGES = ("compile", "simulation", "synthesis")
ERROR_LINE = re.compile(r"^\s*(?:ERROR\s*:|FATAL(?:_ERROR)?\s*:|\[ERROR\]|RTL_AGENT_TB_FAIL:)", re.I | re.M)


def tcl_quote(value):
    value = str(value)
    if "\x00" in value:
        raise AgentError("Tcl 参数不能包含 NUL。")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("[", "\\[").replace("]", "\\]").replace("\n", "\\n").replace("\r", "\\r")
    # Vivado's Windows Tcl source encoding follows the locale. ASCII \u escapes
    # preserve Unicode paths even when UTF-8 source files are read as a legacy codepage.
    escaped = "".join(c if ord(c) < 128 else f"\\u{ord(c):04x}" if ord(c) <= 65535 else f"\\U{ord(c):08x}" for c in escaped)
    return '"' + escaped + '"'


def tcl_path(path):
    return tcl_quote(Path(path).resolve().as_posix())


def clock_constraint_lines(clocks):
    return [f"create_clock -name {tcl_quote(clock['port'])} -period {float(clock['period_ns'])} "
            f"[get_ports {tcl_quote(clock['port'])}]" for clock in clocks]


def _top_declares_port(sources, top, port):
    for source in sources:
        code = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", read_text(source))
        match = re.search(rf"\bmodule\s+(?:(?:automatic|static)\s+)?{re.escape(top)}\b"
                          r"([\s\S]*?)\bendmodule\b", code)
        if not match:
            continue
        body = match.group(1)
        header, separator, declarations = body.partition(";")
        if re.search(rf"\b{re.escape(port)}\b", header):
            return True
        if separator and re.search(rf"\b(?:input|inout)\b[^;]*\b{re.escape(port)}\b",
                                   declarations):
            return True
    return False


def _is_generated_clock_file(path, config):
    expected = "\n".join(clock_constraint_lines(config.clocks)) + "\n"
    try:
        return read_text(path) == expected
    except OSError:
        return False


def find_vivado(value):
    path = Path(value).expanduser()
    found = str(path.resolve()) if path.is_file() else shutil.which(value)
    if not found:
        raise AgentError("找不到 Vivado。请把配置中的 vivado 设为 vivado.bat（Windows）或 vivado（Linux）的完整路径。")
    return found


def validate_work_directory(path):
    path = Path(path).resolve()
    if os.name == "nt" and not str(path).isascii():
        raise AgentError("Vivado/XSim 的运行目录不能包含中文等非 ASCII 字符。无需修改 Windows 用户名；"
                         "请使用 --run-dir F:/codex/rtl_agent/runs/run_01。Python 源码可以留在原目录。")
    return path


def process_command(executable, arguments):
    if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        values = [str(executable), *map(str, arguments)]
        if any(re.search(r'["&|<>^%!\r\n]', arg) for arg in values):
            raise AgentError("Windows 批处理路径含特殊字符。请使用不含 & % ! 等字符的安装/工作路径。")
        # Quote every argument, including paths containing spaces or parentheses.
        command = '"' + " ".join('"' + arg + '"' for arg in values) + '"'
        # Popen(list) applies MS C-runtime escaping, which cmd.exe does not parse.
        # Supply the already-quoted cmd command line as a string instead.
        return subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")]) + " /d /s /c " + command
    return [str(executable), *map(str, arguments)]


def stop_process_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def execute(executable, arguments, directory, stdout_file, timeout):
    command = process_command(executable, arguments)
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    # EDA tools do not need API credentials. Don't propagate known secret variables.
    environment = {key: value for key, value in os.environ.items()
                   if not any(tag in key.upper() for tag in ("API_KEY", "ACCESS_TOKEN", "SECRET"))}
    if os.name == "nt":
        # Vivado initializes its Tcl app store at startup. Keep this per-run cache
        # in the writable work area rather than modifying the user's app store.
        profile = Path(directory) / ".vivado_appdata"
        profile.mkdir(exist_ok=True)
        environment["APPDATA"] = str(profile.resolve())
    temporary = Path(directory) / ".tool_temp"
    temporary.mkdir(exist_ok=True)
    environment["TEMP"] = str(temporary.resolve())
    environment["TMP"] = str(temporary.resolve())
    environment["TMPDIR"] = str(temporary.resolve())
    # Use the installation's read-only Tcl app repository; avoid installing user apps.
    environment["XILINX_LOCAL_USER_DATA"] = "no"
    started = time.monotonic()
    timed_out = False
    with Path(stdout_file).open("wb") as output:
        process = subprocess.Popen(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT,
                                   env=environment, **flags)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process_tree(process)
        except BaseException:
            stop_process_tree(process)
            raise
    return process.returncode, timed_out, time.monotonic() - started


def create_tcl(config, stage, attempt_dir, sources, testbench, constraints):
    project = attempt_dir / "project" / "rtl_agent.xpr"
    body = []
    if stage == "compile":
        body.extend([
            f"create_project rtl_agent {tcl_path(project.parent)} -part {tcl_quote(config.part)}",
            "set_property target_language Verilog [current_project]",

            # 禁止 Vivado 自动重选顶层
            "set_property source_mgmt_mode None [current_project]",
        ])

        for source in sources:
            body.append(f"add_files -norecurse {tcl_path(source)}")

        includes = sorted({
            str(source.parent.resolve()) for source in sources
        } | {
            str((attempt_dir / "rtl").resolve())
        })
        include_list = "[list " + " ".join(tcl_path(p) for p in includes) + "]"

        body.extend([
            f"set_property include_dirs {include_list} [get_filesets sources_1]",
            f"set_property include_dirs {include_list} [get_filesets sim_1]",

            # 先指定仿真使用 RTL 源文件集
            "set_property SOURCE_SET sources_1 [get_filesets sim_1]",

            # 再加入测试台
            f"add_files -fileset sim_1 -norecurse {tcl_path(testbench)}",

            f"set_property xsim.simulate.runtime "
            f"{config.simulation_ns}ns [get_filesets sim_1]",
        ])

        generated_clock_files = [path for path in constraints if _is_generated_clock_file(path, config)]
        active_clocks = [clock for clock in config.clocks
                         if _top_declares_port(sources, config.top, clock["port"])]
        active_clock_file = None
        if generated_clock_files and active_clocks:
            active_clock_file = attempt_dir / "stages" / "compile" / "active_clocks.xdc"
            active_clock_file.parent.mkdir(parents=True, exist_ok=True)
            write_text(active_clock_file, "\n".join(clock_constraint_lines(active_clocks)) + "\n")
        for constraint in constraints:
            if constraint in generated_clock_files:
                if active_clock_file:
                    body.append(f"add_files -fileset constrs_1 -norecurse {tcl_path(active_clock_file)}")
            else:
                body.append(f"add_files -fileset constrs_1 -norecurse {tcl_path(constraint)}")

        body.extend([
            # 先刷新文件和层次结构
            "update_compile_order -fileset sources_1",
            "update_compile_order -fileset sim_1",

            # 刷新完成后再设置顶层
            f"set_property top {tcl_quote(config.top)} [get_filesets sources_1]",
            f"set_property top {tcl_quote(config.tb_top)} [get_filesets sim_1]",

            "launch_simulation -mode behavioral -step compile",
            "launch_simulation -mode behavioral -step elaborate",
        ])
    elif stage == "simulation":
        body.extend([f"open_project {tcl_path(project)}",
                     "launch_simulation -mode behavioral -step simulate",
                     "close_sim"])
    elif stage == "synthesis":
        reports = attempt_dir / "reports"
        reports.mkdir(exist_ok=True)
        body.extend([
            f"open_project {tcl_path(project)}",
            f"synth_design -top {tcl_quote(config.top)} -part {tcl_quote(config.part)}",
        ])
        for clock in config.clocks:
            if not _top_declares_port(sources, config.top, clock["port"]):
                continue
            port = tcl_quote(clock["port"])
            body.extend([
                f"if {{[llength [get_ports -quiet {port}]] != 1}} {{error \"Configured clock port missing\"}}",
                f"if {{[llength [get_clocks -quiet -of_objects [get_ports {port}]]] == 0}} {{error \"Clock constraint was not applied\"}}",
            ])
        body.extend([
            f"write_checkpoint -force {tcl_path(reports / 'synth.dcp')}",
            f"write_verilog -force -mode funcsim {tcl_path(reports / 'netlist.v')}",
            f"report_utilization -file {tcl_path(reports / 'utilization.rpt')}",
            f"report_timing_summary -file {tcl_path(reports / 'timing_summary.rpt')}",
        ])
    else:
        raise AgentError(f"未知 Vivado 阶段：{stage}")
    body.extend(["close_project", f'puts "RTL_AGENT_STAGE_OK:{stage}"'])
    return ("# Generated by rtl_agent. Paths/parameters are escaped, commands are fixed.\n"
            "proc rtl_agent_main {} {\n" + "\n".join("    " + line for line in body) +
            "\n}\nif {[catch {rtl_agent_main} message options]} {\n"
            '    puts stderr "ERROR: RTL_AGENT_TCL_FAILURE: $message"\n'
            '    if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}\n'
            "    exit 1\n}\nexit 0\n")


@dataclass
class StageResult:
    stage: str
    ok: bool
    returncode: int
    timed_out: bool
    seconds: float
    diagnostics: Path
    tcl: Path
    summary: str
    fingerprint: str
    infrastructure_failure: bool = False


def error_fingerprint(stage, text):
    import hashlib
    lines = [line.strip() for line in text.splitlines() if ERROR_LINE.match(line)]
    normalized = "\n".join(lines) or text
    normalized = re.sub(r"attempts[/\\]\d+", "attempts/N", normalized)
    normalized = re.sub(r"revisions[/\\]\d+", "revisions/N", normalized)
    # Fingerprint is only for repeat detection; it NEVER replaces the original diagnostics.
    return hashlib.sha256((stage + "\n" + normalized).encode("utf-8")).hexdigest()


class VivadoRunner:
    def __init__(self, config):
        self.config = config
        self.executable = find_vivado(config.vivado)

    def run(self, stage, attempt_dir, sources, testbench, constraints):
        validate_work_directory(attempt_dir)
        stage_dir = attempt_dir / "stages" / stage
        stage_dir.mkdir(parents=True, exist_ok=False)
        script = stage_dir / "run.tcl"
        write_text(script, create_tcl(self.config, stage, attempt_dir, sources, testbench, constraints))
        stdout = stage_dir / "stdout.log"
        code, timed_out, seconds = execute(
            self.executable, ["-mode", "batch", "-notrace", "-source", str(script),
                              "-log", str(stage_dir / "vivado.log"),
                              "-journal", str(stage_dir / "vivado.jou")],
            attempt_dir, stdout, self.config.stage_timeout_seconds)

        # Preserve ALL logs read for this stage, including complete failure reports.
        logs = [stdout]
        if (stage_dir / "vivado.log").exists():
            logs.append(stage_dir / "vivado.log")
        project_dir = attempt_dir / "project"
        if project_dir.exists():
            logs.extend(sorted(project_dir.rglob("*.log")))
        sections = [f"===== {path.relative_to(attempt_dir).as_posix()} =====\n{read_text(path)}" for path in logs]
        combined = "\n\n".join(sections)
        reasons = []
        if timed_out:
            reasons.append(f"ERROR: 阶段超时（{self.config.stage_timeout_seconds} 秒），已终止本次工具进程树。")
        if code != 0:
            reasons.append(f"ERROR: Vivado 返回码 {code}。")
        if not re.search(rf"(?m)^RTL_AGENT_STAGE_OK:{stage}\s*$", read_text(stdout)):
            reasons.append("ERROR: 未收到 Tcl 阶段完成标记。")
        if ERROR_LINE.search(combined):
            reasons.append("ERROR: 工具日志含 ERROR/FATAL，不能认定成功。")
        if stage == "simulation":
            simulation_logs = [p for p in logs if p.name == "simulate.log" or p.name == "xsim.log"]
            if not any(re.search(rf"(?m)^\s*{PASS_MARKER}\s*$", read_text(p)) for p in simulation_logs):
                reasons.append("ERROR: 仿真未出现独立的 RTL_AGENT_TEST_PASS 标志；进程返回 0 不能证明功能通过。")
        if stage == "synthesis":
            for name in ("netlist.v", "synth.dcp"):
                artifact = attempt_dir / "reports" / name
                if not artifact.is_file() or artifact.stat().st_size == 0:
                    reasons.append(f"ERROR: 缺少非空综合产物 {name}。")
        diagnostics = stage_dir / "diagnostics.txt"
        write_text(diagnostics, combined + ("\n\n===== Agent 判定 =====\n" + "\n".join(reasons) if reasons else ""))
        ok = not reasons
        summary = f"{stage}: {'通过' if ok else '失败'}；耗时 {seconds:.1f}s；完整日志 {diagnostics.relative_to(attempt_dir)}"
        result = StageResult(stage, ok, code, timed_out, seconds, diagnostics, script, summary,
                             error_fingerprint(stage, read_text(diagnostics)),
                             bool(re.search(r"Failed to install all user apps|license.*(?:failed|not found|not available)|"
                                            r"No valid license|specified part.*not found|Project name.*is illegal|"
                                            r"Failed to open file to write|is not recognized as an internal", combined, re.I)))
        atomic_json(stage_dir / "result.json", {"stage": stage, "ok": ok, "returncode": code,
                    "timed_out": timed_out, "seconds": seconds, "summary": summary})
        return result
