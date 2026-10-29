# Sequence detector timing and overlap

Keywords: sequence detector pattern overlap recognition 序列 检测 匹配 重叠

For serial pattern detectors, interpret each state as the matched prefix length. Check the required output cycle and whether overlapping matches are allowed before coding. A detected pulse usually needs a default deassertion on every clock, with a one-cycle assertion on the specified match edge; a sticky flag or an extra output register changes behavior and latency. After a full match, preserve the longest suffix that is also a pattern prefix only when the specification allows overlap. Walk the pattern and a near-match edge by edge, including reset and consecutive matches. Keep the interface and any explicit reset/output-hold contract authoritative.
