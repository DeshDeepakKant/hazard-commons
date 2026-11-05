"""Create .env from .env.example with fresh throwaway testnet keys. Prints ADDRESSES only.

Refuses to overwrite keys already present in .env.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
KEYS = ["DEPLOYER_PRIVATE_KEY", "OPERATOR_ZIPKART_PRIVATE_KEY",
        "OPERATOR_MINUTEMART_PRIVATE_KEY", "OPERATOR_DASHKIRANA_PRIVATE_KEY"]


def main() -> None:
    text = ENV.read_text() if ENV.exists() else EXAMPLE.read_text()
    for k in KEYS:
        m = re.search(rf"^{k}=(.*)$", text, re.M)
        cur = m.group(1).strip() if m else ""
        if cur and cur != "0x...":
            acct = Account.from_key(cur)
            print(f"{k:34s} {acct.address}  (kept existing)")
            continue
        acct = Account.create()
        line = f"{k}={acct.key.hex() if acct.key.hex().startswith('0x') else '0x' + acct.key.hex()}"
        text = re.sub(rf"^{k}=.*$", line, text, flags=re.M) if m else text + "\n" + line + "\n"
        print(f"{k:34s} {acct.address}  (new)")
    ENV.write_text(text)
    ENV.chmod(0o600)
    print(f"\nWrote {ENV} (mode 600). Fund these addresses at https://faucet.0g.ai")


if __name__ == "__main__":
    sys.exit(main())
