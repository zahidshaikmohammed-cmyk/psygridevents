from __future__ import annotations

from .universe import load_instruments


def main() -> None:
    instruments = load_instruments()
    print(f"PSYGRIDEVENTS foundation online: {len(instruments)} instruments configured")
    print("Live source adapters are not enabled in this foundation build.")


if __name__ == "__main__":
    main()
