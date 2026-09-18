## 2026-09-18 - Don't slice raw tool/command output for display then treat the slice as the full result

**Mistake:** Verified `search_github(query="POVQSDTT")` by piping the MCP response through a
Python one-liner that did `print(text[:3500])`. The full tool output (12,744 chars) actually
contained 4 repos (`ARCAD-WISE`, `ARCPB-WISE`, `wss-quotes-processor`, `wss-quotes-service`),
but the truncated display only showed `ARCAD-WISE`'s block (results are grouped alphabetically
by repo name in `search_github`, so `wss-quotes-service` sorted last and fell past the cutoff).
Reported "only ARCAD-WISE uses this table" as if that were the complete, verified result. User
caught it: "I can see it in wss-quotes-service why is it not caught?" - the tool was never wrong,
my own display truncation was.

**Rule:** When verifying a tool/command result is complete (counting matches, listing all repos,
confirming "nothing else uses X"), never slice the displayed output for readability and then treat
that slice as ground truth. Either print the full output, or extract the exact field being verified
programmatically (e.g. regex all `=== repo ===` markers, or `grep -c`) so the "how many / which
ones" answer comes from parsing the complete string, not from what happened to fit before an
arbitrary `[:N]` cutoff.

**Before you write:** Before reporting "only X" / "all of them are Y" / "N total" from a tool
result, check whether the verification step itself truncated, paginated, or `head`-ed the output
for display. If it did, re-derive the count/list from the untruncated data before stating it as
fact.
