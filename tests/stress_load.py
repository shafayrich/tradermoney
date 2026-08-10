#!/usr/bin/env python3
"""
Stress test: boots the real Flask server (threaded) on a test port and hammers
core endpoints under heavy concurrent demand to find limits.
Run: python tests/stress_load.py [--seconds 30] [--workers 40]
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")


def boot_server(port):
    import app as app_mod
    import threading as th

    def _run():
        app_mod.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)

    t = th.Thread(target=_run, daemon=True)
    t.start()
    # wait for readiness
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2).close()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


ENDPOINTS = {
    "/api/config": "GET",
    "/api/status": "GET",
    "/api/broker_status": "GET",
    "/api/license-status": "GET",
    "/api/leaderboard": "GET",
}


def hit(base, path, method="GET", body=None, timeout=10):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return time.perf_counter() - t0, None
    except Exception as e:
        return time.perf_counter() - t0, str(e)


def worker(base, path, method, n, results, stop):
    lat, errs = [], []
    for _ in range(n):
        if stop.is_set():
            break
        dt, e = hit(base, path, method)
        lat.append(dt)
        if e:
            errs.append(e)
    results.append((path, lat, errs))


def _grab_status_error():
    """Reproduce a /api/status error under concurrency to identify the cause."""
    import io
    import traceback
    try:
        import app as app_mod
        import queue as _q
        # drain queue then write an error condition
        while not app_mod.state.ui_queue.empty():
            try:
                app_mod.state.ui_queue.get_nowait()
            except Exception:
                break
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=58123)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--reqs-per-worker", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=0)
    args = ap.parse_args()

    print(f"[stress] booting threaded server on :{args.port} ...")
    boot_server(args.port)
    base = f"http://127.0.0.1:{args.port}"
    print("[stress] server ready.")

    stop = threading.Event()
    results = []
    start = time.perf_counter()
    endpoints = list(ENDPOINTS.items())
    threads = []
    for w in range(args.workers):
        path, method = endpoints[w % len(endpoints)]
        t = threading.Thread(target=worker, args=(base, path, method, args.reqs_per_worker, results, stop), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    print(f"\n[stress] {args.workers} workers x ~{args.reqs_per_worker} reqs in {elapsed:.1f}s")
    by_path = {}
    for path, lat, errs in results:
        by_path.setdefault(path, {"lat": [], "errs": []})
        by_path[path]["lat"].extend(lat)
        by_path[path]["errs"].extend(errs)

    total_reqs = total_errs = 0
    print(f"\n{'endpoint':<26}{'reqs':>7}{'errs':>7}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}")
    for path, m in sorted(by_path.items()):
        lats = sorted(m["lat"])
        n = len(lats)
        errs = len(m["errs"])
        total_reqs += n
        total_errs += errs
        def _pct(arr, p):
            idx = min(len(arr) - 1, int(len(arr) * p))
            return arr[idx] * 1000
        print(f"{path:<26}{n:>7}{errs:>7}{_pct(lats,0.5):>8.1f}ms{_pct(lats,0.95):>8.1f}ms{_pct(lats,0.99):>8.1f}ms{lats[-1]*1000:>8.1f}ms")
        if m["errs"]:
            from collections import Counter
            kinds = Counter(e.split(":")[0] for e in m["errs"][:50])
            print(f"    errors: {dict(kinds)} sample={m['errs'][0][:120]!r}")

    print(f"\nTOTAL: {total_reqs} reqs, {total_errs} errors ({total_errs/max(total_reqs,1)*100:.2f}%)")
    if total_errs == 0:
        print("RESULT: PASS - no errors under load")
    else:
        print("RESULT: LIMITS FOUND - see errors above")
        sys.exit(1)


if __name__ == "__main__":
    main()