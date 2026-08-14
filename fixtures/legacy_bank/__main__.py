"""Run the legacy bank fixture app: python -m fixtures.legacy_bank --port 5055"""

import argparse

from fixtures.legacy_bank.app import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the legacy bank fixture app.")
    parser.add_argument("--port", type=int, default=5055)
    args = parser.parse_args()
    print(f" * legacy_bank fixture serving on http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
