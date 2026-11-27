import argparse
import asyncio
from .host import serve

parser = argparse.ArgumentParser()
parser.add_argument("--data-dir", required=True)
parser.add_argument("--fixture", action="store_true")
args = parser.parse_args()
asyncio.run(serve(args.data_dir, args.fixture))
