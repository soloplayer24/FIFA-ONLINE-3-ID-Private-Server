"""One command to run every local test. No client, no two terminals, no strays.

    python run_tests.py

Runs: blaze self-test, analyze_capture self-test, then starts the server on test
ports, runs the loopback client against it, and stops the server. Prints PASS/FAIL.
This only proves the server is internally consistent — a real FO3 client capture
(blocked on XignCode) is still what validates the protocol. See README.
"""
import os, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
R, N = 39000, 39001   # test ports (avoid the real 28000/28001)


def step(name, argv):
    print(f"\n=== {name} ===")
    r = subprocess.run([PY, *argv], cwd=HERE)
    return r.returncode == 0


def wait_port(port, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    results = []
    results.append(("blaze", step("blaze self-test", ["blaze.py"])))
    results.append(("analyze_capture", step("analyze_capture self-test", ["analyze_capture.py", "--selftest"])))
    results.append(("league", step("league self-test", ["test_league.py"])))

    print("\n=== loopback (server + test_client) ===")
    srv = subprocess.Popen([PY, "-u", "server.py", str(R), str(N)], cwd=HERE,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = wait_port(R)
        if not ok:
            print("server did not come up")
            results.append(("loopback", False))
        else:
            results.append(("loopback", subprocess.run([PY, "test_client.py", str(R)], cwd=HERE).returncode == 0))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()

    print("\n==== SUMMARY ====")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    allok = all(ok for _, ok in results)
    print("ALL PASS" if allok else "SOME FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
