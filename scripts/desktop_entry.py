"""Entry point for the standalone desktop executable."""
import multiprocessing
from agent_factory.desktop import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
