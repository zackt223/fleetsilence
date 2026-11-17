"""Print a /job-cover answer from a running service as a short table.

    python3 tools/show_cover.py MSS 2026-10-31T23:59:00Z 72 [http://127.0.0.1:8080]
"""
import json
import sys
from urllib.request import urlopen


def main():
    depot, at, hours = sys.argv[1:4]
    base = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8080"
    with urlopen(f"{base}/job-cover?depot={depot}&at={at}&hours={hours}") as r:
        body = json.load(r)
    print(body["summary"])
    for j in body["jobs"]:
        print(f"{j['job_id']}  {j['asset_id']:6} {j['problem']:12} {j['decision']:5} {j['swap_to'] or '':6}")
        print(f"    {j['reasons'][-1]}")


if __name__ == "__main__":
    main()
