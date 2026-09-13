# sym_lists — notes for Claude

Symbol lists consumed by the datalog cron. Each `*.txt` is one symbol per line.

## hyperliquid.txt structure
- Plain names (`BTC`, `ETH`, ...) = standard Hyperliquid perps.
- `@N` (e.g. `@107`) = spot indices.
- `PURR/USDC` = spot pair.
- `xyz:NAME` = HIP-3 builder-deployed perps on the **`xyz`** DEX. We only track the `xyz`
  deployer here (not the other live HIP-3 DEXs: flx, vntl, hyna, km, abcd, cash, para, mkts).
- Keep `xyz:` entries alphabetically sorted.

## Checking completeness against the live chain
HIP-3 markets change over time. To diff the file against the live `xyz` universe:

```python
import json, urllib.request
def info(p):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info",
        data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))

# {"type":"perpDexs"} -> all deployed HIP-3 DEXs (name/deployer/...)
meta = info({"type": "meta", "dex": "xyz"})          # xyz universe; names already "xyz:..."
live = {a["name"] for a in meta["universe"]}
delisted = {a["name"] for a in meta["universe"] if a.get("isDelisted")}
```

Compare `live` against the `xyz:` lines in the file. Note: the live meta keeps **delisted**
markets in the universe (`isDelisted: true`) — they aren't actively trading, so missing ones
aren't necessarily worth adding (e.g. `xyz:IBIDEN` was added 2026-06-30 despite being delisted).

## Syncing to gfsplit
The cron also runs on the `gfsplit` host (ssh alias; Tokyo EC2). Its copy lives at
**`~/cron/sym_lists/`** (NOT the full repo path). After editing, push:

```bash
scp <file> gfsplit:cron/sym_lists/<file>
ssh gfsplit 'grep -c "^xyz:" cron/sym_lists/hyperliquid.txt'   # verify
```
