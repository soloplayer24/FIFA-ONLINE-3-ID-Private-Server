"""Start the FO3ID local servers together: Blaze server.py + http_config.py.

Streams both outputs prefixed, and on Ctrl-C (or --selftest) terminates both so
nothing is left listening.

Usage:
    python start_all.py [http_port=8080]
    python start_all.py --selftest [http_port]   # start, verify alive ~2s, stop, exit
"""
import os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
args = [a for a in sys.argv[1:]]
selftest = "--selftest" in args
args = [a for a in args if a != "--selftest"]
http_port = args[0] if args else "8080"

procs = []


def pump(name, proc):
    for line in proc.stdout:
        sys.stdout.write(f"[{name}] {line}")
        sys.stdout.flush()


def start(name, extra):
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, name), *extra],
                         cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    threading.Thread(target=pump, args=(name, p), daemon=True).start()
    procs.append((name, p))


def shutdown():
    for name, p in procs:
        if p.poll() is None:
            p.terminate()
    for name, p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    start("server.py", [])                 # redirector :28000 + notification :28001
    start("http_config.py", [http_port])   # config endpoints
    start("http_config.py", ["443", "--tls"])  # https://APSEODESK172 (hardcoded in fifazf.exe)
    start("messenger.py", [])  # :28002, address handed out by /info/my/services/messenger/2
    print(f"[start_all] server.py + http_config.py (:{http_port}, :443 https) up. Ctrl-C to stop.")

    if selftest:
        time.sleep(2)
        alive = all(p.poll() is None for _, p in procs)
        shutdown()
        print("[start_all] selftest", "OK" if alive else "FAILED")
        sys.exit(0 if alive else 1)

    try:
        while all(p.poll() is None for _, p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        print("[start_all] stopped.")


if __name__ == "__main__":
    main()
