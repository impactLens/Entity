#!/usr/bin/env python3
"""Simulate concurrent users against /v1/extract (standard library only).

  python loadtest.py --url http://127.0.0.1:8000 --key SECRET \
      --users 10 --requests 5 --chars 20000
  python loadtest.py ... --long 200000      # one very long text per request

Exit code 0 = no errors and p95 under --p95-limit seconds.
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

SAMPLE = (
    "On March 3rd, 2026, Dr. Amina Yusuf of Oxford University presented a "
    "literature review at 9:30 AM UTC in Geneva. Her research question examined "
    "how ransomware campaigns affect national security in Germany and Japan. "
    "Professor Daniel Okafor from the University of Lagos argued that sanctions "
    "and soft power shape policy more than any treaty. By 14:05 EST the "
    "delegates from NATO had moved to Brussels for an emergency summit. "
)


def make_texts(chars, nodes):
    """Either `nodes` short texts (like page text nodes) or one long text."""
    if nodes <= 1:
        return [(SAMPLE * (chars // len(SAMPLE) + 1))[:chars]]
    size = max(40, chars // nodes)
    return [(SAMPLE * (size // len(SAMPLE) + 1))[:size] for _ in range(nodes)]


def one_request(url, key, texts, timeout):
    body = json.dumps({"texts": texts}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/extract", data=body,
        headers={"Content-Type": "application/json", "X-API-Key": key})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception:
        code = "conn"
    return code, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", default="")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--requests", type=int, default=5, help="per user")
    ap.add_argument("--chars", type=int, default=20000, help="chars per request")
    ap.add_argument("--nodes", type=int, default=40, help="texts per request")
    ap.add_argument("--long", type=int, default=0, metavar="CHARS",
                    help="send one text of this many chars instead")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--p95-limit", type=float, default=30)
    a = ap.parse_args()

    texts = make_texts(a.long, 1) if a.long else make_texts(a.chars, a.nodes)
    print(f"{a.users} users x {a.requests} requests, "
          f"{sum(map(len, texts)):,} chars in {len(texts)} text(s) each")

    results, lock = [], threading.Lock()
    barrier = threading.Barrier(a.users)

    def user():
        barrier.wait()                       # everyone starts together
        for _ in range(a.requests):
            r = one_request(a.url, a.key, texts, a.timeout)
            with lock:
                results.append(r)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=user) for _ in range(a.users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    lat = sorted(l for c, l in results if c == 200)
    bad = {}
    for c, _ in results:
        if c != 200:
            bad[c] = bad.get(c, 0) + 1
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else float("nan")
    print(f"ok {len(lat)}/{len(results)}   errors {bad or 'none'}")
    print(f"throughput {len(results) / wall:.2f} req/s   wall {wall:.1f}s")
    if lat:
        print(f"latency  p50 {statistics.median(lat):.2f}s   "
              f"p95 {pct(0.95):.2f}s   max {lat[-1]:.2f}s")
    passed = not bad and lat and pct(0.95) <= a.p95_limit
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
