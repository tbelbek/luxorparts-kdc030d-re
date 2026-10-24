#!/usr/bin/env python3
"""Cross-reference / call-graph index for an or1k_dis.py disassembly listing.

Standard decompilers (Ghidra/radare2) assume the stock OpenRISC delay slot; this SoC does
NOT (l.jal links r9=PC+4, a taken branch skips the next instruction), so their call-adjacent
analysis is wrong here. This works off our own correct listing instead.

Build the listing first:  python3 or1k_dis.py DUMP 0x01ffdc00 0x2400 0x7ea00 > app.dis
Then:
  xref.py app.dis callers 0x0202c690   # every call site that targets this address
  xref.py app.dis calls   0x02039000   # every call made by the function starting here
  xref.py app.dis func    0x0202c6d0   # which function (entry) contains this address
  xref.py app.dis trace   0x0202c690   # walk callers upward (who calls, who calls them, ...)
  xref.py app.dis stats                # counts
"""
import re, sys
from collections import defaultdict

LINE = re.compile(r'^([0-9a-f]{8}):\s+[0-9a-f]{8}\s+l\.(jal|jalr|j|bf|bnf|jr)\b(.*)$')
TARGET = re.compile(r'0x([0-9a-f]{8})')


def load(path):
    addrs = []                       # every instruction address, in order
    calls = []                       # (site, target) for l.jal (direct calls)
    for ln in open(path):
        m = re.match(r'^([0-9a-f]{8}):', ln)
        if not m:
            continue
        a = int(m.group(1), 16); addrs.append(a)
        mm = LINE.match(ln.strip())
        if mm and mm.group(2) == 'jal':
            t = TARGET.search(mm.group(3))
            if t:
                calls.append((a, int(t.group(1), 16)))
    entries = sorted(set(t for _, t in calls))          # call targets = function entry points
    callers = defaultdict(list)
    for s, t in calls:
        callers[t].append(s)
    return addrs, calls, entries, callers


def func_of(entries, addr):
    lo = 0; hi = len(entries); import bisect
    i = bisect.bisect_right(entries, addr) - 1
    return entries[i] if i >= 0 else None


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    path, cmd = sys.argv[1], sys.argv[2]
    addrs, calls, entries, callers = load(path)
    if cmd == 'stats':
        print(f'instructions {len(addrs)}  direct-calls {len(calls)}  functions(entries) {len(entries)}')
    elif cmd == 'callers':
        t = int(sys.argv[3], 16)
        cs = sorted(callers.get(t, []))
        print(f'{len(cs)} call site(s) target 0x{t:08x}:')
        for s in cs:
            print(f'  0x{s:08x}  (in func 0x{func_of(entries, s):08x})')
    elif cmd == 'calls':
        f = int(sys.argv[3], 16)
        nxt = entries[entries.index(f) + 1] if f in entries and entries.index(f) + 1 < len(entries) else 0xffffffff
        made = sorted(set(t for s, t in calls if f <= s < nxt))
        print(f'func 0x{f:08x} calls {len(made)} distinct target(s):')
        for t in made:
            print(f'  0x{t:08x}')
    elif cmd == 'func':
        a = int(sys.argv[3], 16)
        print(f'0x{a:08x} is in function 0x{func_of(entries, a):08x}')
    elif cmd == 'trace':
        t = int(sys.argv[3], 16); seen = set(); depth = int(sys.argv[4]) if len(sys.argv) > 4 else 4
        def walk(target, d, prefix):
            f = func_of(entries, target)
            cs = sorted(set(func_of(entries, s) for s in callers.get(target, [])))
            print(f'{prefix}0x{target:08x} (func 0x{(f or 0):08x})  <- {len(callers.get(target, []))} site(s) in {len(cs)} func(s)')
            if d <= 0 or target in seen:
                return
            seen.add(target)
            for cf in cs:
                if cf is not None:
                    walk(cf, d - 1, prefix + '  ')
        walk(t, depth, '')
    else:
        print(__doc__); sys.exit(2)


if __name__ == '__main__':
    main()
