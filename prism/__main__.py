import argparse

import uvicorn

from . import NAME, app as portal


def main() -> None:
    p = argparse.ArgumentParser(prog="prism", description=f"{NAME} portal")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to let colleagues on the network open it")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()
    portal.BIND["host"] = args.host
    print(f"{NAME}: http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}")
    uvicorn.run(portal.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
