import os
import sys


def log(message):
    print(message, flush=True)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: worker.py <fifo>")
    fifo = sys.argv[1]
    state_dir = os.path.dirname(fifo)
    done_path = os.path.join(state_dir, "worker.done")

    if os.environ.get("AUTO_CONFIRM") == "1":
        token = "auto"
        log("AUTO_CONFIRM=1 set; skipping input.")
    else:
        log(f"Waiting for input on {fifo} ...")
        with open(fifo, "r", encoding="utf-8") as handle:
            token = handle.readline().strip()

    log(f"Received: {token}")
    with open(done_path, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
    log("Done.")


if __name__ == "__main__":
    main()
