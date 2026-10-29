"""Task-local verification cache keyed by stage, top and exact source hash.

No persistent or cross-task cache: toolchain and settings are fixed during one
solve. Transient resource/unavailable results are never reused.
"""
from __future__ import annotations

import hashlib
import re


class VerificationCache:
    def __init__(self, toolchain):
        self.toolchain = toolchain
        self.results = {}

    def check(self, stage: str, source: str, top: str, timeout_s: float
              ) -> tuple[int, str, bool]:
        if stage not in {"lint", "synth"}:
            raise ValueError("Unsupported verification stage")
        key = (stage, top, hashlib.sha256(source.encode("utf-8")).hexdigest())
        if key in self.results:
            rc, log = self.results[key]
            return rc, log, True
        rc, log = getattr(self.toolchain, stage)(source, top, timeout_s=timeout_s)
        transient = re.search(r"timed?\s*out|timeout|budget exhausted|could not launch|"
                              r"licen[cs]|server connection|killed", log, re.I)
        if rc >= 0 and not transient:
            self.results[key] = (rc, log)
        return rc, log, False
