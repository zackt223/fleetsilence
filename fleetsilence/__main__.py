"""Start the service:  python3 -m fleetsilence"""
import os

from .server import serve


def main():
    data_dir = os.environ.get("QF_DATA_DIR") or "./data"
    serve(data_dir, host=os.environ.get("QF_HOST"), port=os.environ.get("QF_PORT"))


if __name__ == "__main__":
    main()
