#!/usr/bin/env python3
"""Compatibility entrypoint; source policy and its reviewed parser are owned by Tools."""
import os
from pathlib import Path
import sys

root = Path(os.environ['TOOLS_ROOT'])
driver = root / 'cookbook/source_policy.py'
if not driver.is_file():
    raise SystemExit('The reviewed Tools source-policy SDK is required; run contributor setup first')
os.execv(sys.executable, [sys.executable, '-I', str(driver), 'check', '--sdk-root', str(Path(__file__).resolve().parents[1]), *sys.argv[1:]])
