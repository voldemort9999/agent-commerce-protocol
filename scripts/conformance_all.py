"""Wahi ek conformance suite, HAR registered merchant par.

    .venv/Scripts/python.exe scripts/conformance_all.py

Suite ko ek line bhi nahi chhua jata. Wo jaan-boojhkar ek waqt me ek merchant par chalti
hai — uska `--base-url` ek CLI option hai, aur usme merchants ki suchi ghusa dena use us
project ka hissa bana deta jise wo test kar rahi hai. Ye script bas use ginti baar
chalati hai, `layer/merchants.json` padhkar.

Ye zaroori isliye hai ki root se seedha `pytest` chalane par conformance sirf apne
DEFAULT base-url (8001) par chalti hai — yaani ek reviewer jo `pytest` type karta hai
use Merchant B ki conformance dikhti hi nahi, aur suite green rehti hai. Wahi "chup-chaap
kam kaam hua" wali shakal jo is project me baar-baar mili hai.
"""
import json
import os
import pathlib
import subprocess
import sys

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def main():
    config = json.loads((ROOT / "layer" / "merchants.json").read_text(encoding="utf-8"))
    extra = sys.argv[1:]
    results = []

    for merchant in config:
        key = os.getenv(merchant["key_env"])
        if not key:
            results.append((merchant["merchant_id"], "NO KEY", merchant["key_env"]))
            continue
        print("=" * 72)
        print(f"{merchant['merchant_id']}  ->  {merchant['base_url']}")
        print("=" * 72, flush=True)
        # Har merchant ka apna delivery footprint hota hai, aur wo manifest me kahin
        # nahi hai — SPEC me aisa koi field hai bhi nahi ("ek pincode jo aap nahi
        # bhejte" ek asli merchant ke liye bemaani hai). Suite ise GUESS nahi kar
        # sakti: agar wo galat pincode maan le to serviceability wale test us merchant
        # par **galat wajah se** laal ya green honge. Isliye ye ek declared fact hai,
        # aur suite ka apna `--dead-pincode` option pehle se maujood hai — suite badalti
        # nahi, sirf use sach bataya jata hai.
        #   Northwind aur Voltline dono `7` mana karte hain; Marigold `7` BHEJTA hai
        #   (aur `6` nahi), isliye uska dead pincode alag hai.
        conf = merchant.get("conformance", {})
        options = []
        if conf.get("pincode"):
            options += ["--pincode", conf["pincode"]]
        if conf.get("dead_pincode"):
            options += ["--dead-pincode", conf["dead_pincode"]]
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "spec/conformance", "-q",
             "--base-url", merchant["base_url"], "--agent-key", key, "-rs",
             *options, *extra],
            cwd=ROOT)
        results.append((merchant["merchant_id"], "PASS" if completed.returncode == 0
                        else f"FAIL (exit {completed.returncode})", merchant["base_url"]))

    print("\n" + "=" * 72)
    for merchant_id, verdict, detail in results:
        print(f"  {verdict:16s} {merchant_id:24s} {detail}")
    failed = [r for r in results if r[1] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} merchants conformant")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
